"""
LongMemEval evaluation of the human-in-the-loop memory layer.

Dataset: LongMemEval (Wu et al., ICLR 2025), oracle version: user <-> assistant chats,
each question with only the sessions that contain its evidence.
    https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned  -> longmemeval_oracle.json

How it works (per question, with a FRESH memory store each time):
  1. REPLAY  the recorded chat, session by session in date order. After each
             (user, assistant) turn pair, the app's own pipeline runs: extraction
             prompt from llm/prompts.py -> add / update / skip / conflict.
             Nothing is generated mid-conversation, so the chat cannot drift.
  2. ANSWER  the question from retrieved memories only -- once with NO HUMAN
             (pending conflicts unresolved, old value kept), and once after a
             SIMULATED USER accepts every pending conflict (one click each).
  3. JUDGE   with the official LongMemEval judge prompts (per question type).
  4. For knowledge-update questions, LOCATE the update: the evidence turns
     (has_answer=true) in the latest evidence session. Record what the system
     did there: asked (conflict) / silently overwrote / kept both / missed.

Usage (from the project folder):
    python -m evaluation.longmemeval_eval --data longmemeval_oracle.json --per-type 5      # quick cost check
    python -m evaluation.longmemeval_eval --data longmemeval_oracle.json --per-type 40
    python -m evaluation.longmemeval_eval --data ... --policy overwrite                    # ablation
    python -m evaluation.longmemeval_eval --data ... --dry-run                              # no API calls

Every prompt sent to the API and every output is saved to results/trace_<tag>.md
(readable) and results/trace_<tag>.jsonl (complete), grouped by question.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from evaluation.locomo_eval import EvalLLM, fresh_store
from llm.prompts import build_extraction_messages
from memory.models import EventType, MemoryStatus
from strategies.baseline import BaselineStrategy

# gpt-oss-20b on Groq, USD per 1M tokens (for the cost estimate only)
PRICE_IN, PRICE_OUT = float(os.environ.get("PRICE_IN", 0.075)), float(os.environ.get("PRICE_OUT", 0.30))

ANSWER_PROMPT = """You are a helpful assistant with long-term memory of the user.
Today's date: {date}

What you remember about the user (each with the date it was last updated):
{memories}

Answer the user's question using only what you remember. Be brief.
If two memories disagree, the more recent one is the current state.
If your memories do not contain the answer, say you don't know.

User: {question}"""

# ---- official LongMemEval judge prompts (src/evaluation/evaluate_qa.py) -----------
_BASE = ("I will give you a question, a correct answer, and a response from a model. Please answer yes "
         "if the response contains the correct answer. Otherwise, answer no. If the response is equivalent "
         "to the correct answer or contains all the intermediate steps to get the correct answer, you should "
         "also answer yes. If the response only contains a subset of the information required by the answer, "
         "answer no. ")
JUDGE = {
    "default": _BASE + "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "temporal-reasoning": _BASE + "In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "knowledge-update": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "single-session-preference": "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "abstention": "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only.",
}


def judge_prompt(qtype: str, abstention: bool) -> str:
    if abstention:
        return JUDGE["abstention"]
    return JUDGE.get(qtype, JUDGE["default"])


class FakeLLM:
    """--dry-run stand-in: lets the whole pipeline run offline."""

    def __init__(self):
        self.calls = 0
        self.inner = type("X", (), {"usage": {"prompt_tokens": 0, "completion_tokens": 0}})()
        self.model = "fake"

    def chat(self, messages, **kw):
        self.calls += 1
        return random.choice(["yes", "no"]) if "Answer yes or no" in messages[-1]["content"] else "I think so."

    def json_completion(self, messages, **kw):
        self.calls += 1
        text = messages[-1]["content"]
        if "EXISTING memories:" in text:
            return {"action": random.choice(["add", "update", "conflict", "skip"]), "target": 1,
                    "confidence": 0.9, "options": ["keep new"]}
        user_line = text.split("\n")[0][len("User said: "):]
        return {"memories": [{"content": "User: " + user_line[:80], "type": "fact"}]}


# --------------------------------------------------------------- call trace
# Every prompt sent to the API and every output is saved, so you can check them:
#   results/trace_<tag>.md     readable, grouped by question (long system prompts
#                              are printed in full once, then referred to by name)
#   results/trace_<tag>.jsonl  complete, one JSON object per call

def call_kind(messages) -> str:
    text = " ".join(m.get("content", "") for m in messages)
    if "You extract durable facts" in text:
        return "extraction"
    if "You manage a long-term memory" in text:
        return "memory decision (add/update/skip/conflict)"
    if "You compare a NEW candidate memory" in text:
        return "conflict check"
    if "Answer yes or no only" in text:
        return "judge"
    if "What you remember about the user" in text:
        return "answer"
    return "other"


class TracedLLM:
    """Wraps the real/fake LLM and logs each call; everything else is forwarded."""

    def __init__(self, llm, out_dir: Path, tag: str):
        self._llm = llm
        self.md_path = Path(out_dir) / f"trace_{tag}.md"
        self.jsonl_path = Path(out_dir) / f"trace_{tag}.jsonl"
        self.md_path.write_text(f"# LLM call trace — {tag}\n\n", encoding="utf-8")
        self.jsonl_path.write_text("", encoding="utf-8")
        self.context = ""
        self._n = 0
        self._shown_system = set()
        self._last_context = None

    def __getattr__(self, name):          # .inner, .calls, .model ...
        return getattr(self._llm, name)

    def chat(self, messages, **kw):
        out = self._llm.chat(messages, **kw)
        self._log(messages, out)
        return out

    def json_completion(self, messages, **kw):
        out = self._llm.json_completion(messages, **kw)
        self._log(messages, out)
        return out

    def _log(self, messages, output):
        self._n += 1
        kind = call_kind(messages)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"n": self._n, "context": self.context, "kind": kind,
                                "messages": messages, "output": output}, ensure_ascii=False) + "\n")
        lines = []
        if self.context != self._last_context:
            lines.append(f"\n---\n\n## {self.context}\n")
            self._last_context = self.context
        lines.append(f"### #{self._n} · {kind}\n")
        for m in messages:
            role, content = m.get("role", "?").upper(), m.get("content", "")
            if role == "SYSTEM" and kind in self._shown_system:
                lines.append(f"**SYSTEM:** _(same {kind} prompt as shown earlier)_\n")
                continue
            if role == "SYSTEM":
                self._shown_system.add(kind)
            lines.append(f"**{role}:**\n```text\n{content}\n```\n")
        shown = json.dumps(output, indent=1, ensure_ascii=False) if isinstance(output, (dict, list)) else str(output)
        lines.append(f"**→ OUTPUT:**\n```text\n{shown}\n```\n")
        with open(self.md_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------- helpers

def ordered_sessions(entry: dict):
    """Oracle sessions are NOT sorted by time in the file -- sort them by date."""
    triples = list(zip(entry["haystack_dates"], entry["haystack_session_ids"], entry["haystack_sessions"]))
    return sorted(triples, key=lambda t: t[0])


def turn_pairs(session: list):
    """Yield (pair_index, user_text, assistant_text, has_answer) for a session."""
    i, k = 0, 0
    while i < len(session):
        t = session[i]
        if t.get("role") != "user":
            i += 1
            continue
        nxt = session[i + 1] if i + 1 < len(session) and session[i + 1].get("role") == "assistant" else None
        has_answer = bool(t.get("has_answer")) or bool(nxt and nxt.get("has_answer"))
        yield k, t.get("content", ""), (nxt or {}).get("content", ""), has_answer
        k += 1
        i += 2 if nxt else 1


def as_bullets(items):
    return "\n".join(f"- {x}" for x in items) if items else "(nothing yet)"


def answer_and_judge(llm, strategy, entry, top_k, abstention, mem_dates=None):
    retrieved = strategy.retrieve_for_query(entry["question"], top_k=top_k)
    mem_dates = mem_dates or {}
    mems = [f"[{mem_dates.get(m.id, '?')[:10]}] {m.content}" for m, _ in retrieved]
    pred = llm.chat([{"role": "user", "content": ANSWER_PROMPT.format(
        date=entry.get("question_date", "unknown"), memories=as_bullets(mems), question=entry["question"])}],
        temperature=0.0, max_tokens=400) or ""
    verdict = llm.chat([{"role": "user", "content": judge_prompt(entry["question_type"], abstention).format(
        entry["question"], entry["answer"], pred)}], temperature=0.0, max_tokens=200) or ""
    return pred.strip(), "yes" in verdict.lower(), mems


# ---------------------------------------------------------------------- main

def run_question(entry, llm, policy, top_k, max_assistant_chars):
    store = fresh_store()
    strategy = BaselineStrategy(store, llm=llm, non_conflict_policy=policy)
    abstention = entry["question_id"].endswith("_abs")
    sessions = ordered_sessions(entry)

    # the update = evidence turns in the LATEST session that contains evidence
    evidence_sessions = [sid for _, sid, sess in sessions
                         if any(t.get("has_answer") for t in sess)]
    update_session = evidence_sessions[-1] if evidence_sessions else None

    def ctx(text):
        if hasattr(llm, "md_path"):
            llm.context = f"{entry['question_id']} ({entry['question_type']}) — {text}"

    update_turn_events, all_events, n_pairs = [], [], 0
    conflict_dates = {}
    mem_dates = {}   # memory id -> chat date of its last change (the replay happens "today")
    for date, sid, sess in sessions:
        for k, user, assistant, has_answer in turn_pairs(sess):
            mark = "  ⭐ EVIDENCE TURN" + (" (the UPDATE)" if sid == update_session else "") if has_answer else ""
            ctx(f"replay session {sid} [{date}], turn pair {k}{mark}")
            n_pairs += 1
            try:
                out = llm.json_completion(build_extraction_messages(user, assistant[:max_assistant_chars]))
            except Exception as e:
                print(f"    extraction failed: {e}")
                continue
            cands = out.get("memories", []) if isinstance(out, dict) else []
            evs = strategy.apply_candidates(f"lme_{entry['question_id']}", cands,
                                            source=f"{sid} [{date}] pair {k}")
            all_events += evs
            for ev in evs:
                if ev.memory_id and ev.type in (EventType.SAVED, EventType.UPDATED):
                    mem_dates[ev.memory_id] = date
                elif ev.type == EventType.CONFLICT:
                    conflict_dates[ev.id] = date
            if has_answer and sid == update_session:
                update_turn_events += evs

    pending = store.events.pending_conflicts()

    # (a) no human: conflicts stay pending, old memory kept
    ctx(f"ANSWER without human  |  question: {entry['question']}  |  gold: {entry['answer']}")
    pred_a, ok_a, mems_a = answer_and_judge(llm, strategy, entry, top_k, abstention, mem_dates)

    # (b) simulated user: accepts the new information for every pending conflict
    for evt in pending:
        try:
            strategy.resolve_conflict(evt.id, evt.after)
            mem_dates[evt.memory_id] = conflict_dates.get(evt.id, mem_dates.get(evt.memory_id, "?"))
        except Exception:
            pass
    if pending:
        ctx(f"ANSWER after simulated user accepted {len(pending)} conflict(s)")
        pred_b, ok_b, mems_b = answer_and_judge(llm, strategy, entry, top_k, abstention, mem_dates)
    else:
        pred_b, ok_b, mems_b = pred_a, ok_a, mems_a

    kinds = {e.type for e in update_turn_events}
    if EventType.CONFLICT in kinds:
        handling = "asked user (conflict)"
    elif EventType.UPDATED in kinds:
        handling = "silent overwrite"
    elif EventType.SAVED in kinds:
        handling = "kept both (added)"
    else:
        handling = "missed / skipped"

    return {
        "question_id": entry["question_id"],
        "type": entry["question_type"] + ("+abs" if abstention else ""),
        "question": entry["question"], "gold": str(entry["answer"]),
        "n_sessions": len(sessions), "n_turn_pairs": n_pairs,
        "conflicts_raised": len(pending),
        "update_turn_handling": handling if entry["question_type"] == "knowledge-update" else "",
        "pred_no_human": pred_a, "correct_no_human": int(ok_a),
        "pred_with_human": pred_b, "correct_with_human": int(ok_b),
        "active_memories": len(store.list(status=MemoryStatus.ACTIVE)),
        "retrieved_no_human": " || ".join(mems_a),
    }


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="path to longmemeval_oracle.json")
    ap.add_argument("--types", default="knowledge-update,single-session-user",
                    help="comma-separated question types")
    ap.add_argument("--per-type", type=int, default=5, help="questions sampled per type (0 = all)")
    ap.add_argument("--abstention", action="store_true", help="also include the *_abs questions of those types")
    ap.add_argument("--policy", choices=["decision", "relation", "overwrite"], default="decision")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--max-assistant-chars", type=int, default=600,
                    help="assistant replies are truncated for extraction (memories are about the user)")
    ap.add_argument("--model", default=os.environ.get("EVAL_MODEL", "openai/gpt-oss-20b"))
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="results")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    random.seed(args.seed)
    data = json.load(open(args.data, encoding="utf-8"))
    types = [t.strip() for t in args.types.split(",")]

    chosen = []
    for t in types:
        pool = [e for e in data if e["question_type"] == t
                and (args.abstention or not e["question_id"].endswith("_abs"))]
        if args.per_type and len(pool) > args.per_type:
            pool = random.sample(pool, args.per_type)
        chosen += pool
    print(f"== LongMemEval: {len(chosen)} questions  types={types}  policy={args.policy}  "
          f"model={'fake' if args.dry_run else args.model}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"lme_{args.policy}" + ("_dry" if args.dry_run else "")
    llm = TracedLLM(FakeLLM() if args.dry_run else EvalLLM(args.model, args.sleep), out_dir, tag)

    rows, t0 = [], time.time()
    for i, entry in enumerate(chosen, 1):
        try:
            r = run_question(entry, llm, args.policy, args.top_k, args.max_assistant_chars)
        except Exception as e:
            print(f"  [{i}/{len(chosen)}] ERROR on {entry['question_id']}: {e}")
            continue
        rows.append(r)
        u = llm.inner.usage
        cost = (u["prompt_tokens"] * PRICE_IN + u["completion_tokens"] * PRICE_OUT) / 1e6
        print(f"  [{i:>3}/{len(chosen)}] {r['type']:<20} no-human {'✓' if r['correct_no_human'] else '✗'}  "
              f"with-human {'✓' if r['correct_with_human'] else '✗'}  conflicts={r['conflicts_raised']}  "
              f"{r['update_turn_handling']:<22} | spent ≈ ${cost:.3f}")
        # write as we go, so a crash / rate-limit stop loses nothing
        with open(out_dir / f"qa_{tag}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    if not rows:
        print("no results")
        return

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    ku = [r for r in rows if r["type"] == "knowledge-update"]
    ctrl = [r for r in rows if r["type"] != "knowledge-update"]
    u = llm.inner.usage
    summary = {
        "policy": args.policy, "model": "fake" if args.dry_run else args.model, "n": len(rows),
        "by_type": {t: {"n": len(v),
                        "acc_no_human": round(sum(r["correct_no_human"] for r in v) / len(v), 3),
                        "acc_with_human": round(sum(r["correct_with_human"] for r in v) / len(v), 3),
                        "avg_conflicts_per_question": round(sum(r["conflicts_raised"] for r in v) / len(v), 2)}
                    for t, v in sorted(by_type.items())},
        "knowledge_update_handling": dict(Counter(r["update_turn_handling"] for r in ku)),
        "questions_asked_to_user_total": sum(r["conflicts_raised"] for r in rows),
        "control_questions_with_any_conflict": sum(1 for r in ctrl if r["conflicts_raised"]),
        "tokens": u, "approx_cost_usd": round((u["prompt_tokens"] * PRICE_IN + u["completion_tokens"] * PRICE_OUT) / 1e6, 4),
        "llm_calls": llm.calls, "minutes": round((time.time() - t0) / 60, 1),
    }
    with open(out_dir / f"summary_{tag}.json", "w") as f:
        json.dump(summary, f, indent=2)

    print("\n=================== SUMMARY ===================")
    print(f"{'type':<28}{'n':>4}{'no human':>11}{'with human':>12}{'asks/q':>8}")
    for t, s in summary["by_type"].items():
        print(f"{t:<28}{s['n']:>4}{s['acc_no_human']:>11.0%}{s['acc_with_human']:>12.0%}"
              f"{s['avg_conflicts_per_question']:>8}")
    if ku:
        print(f"\nAt the knowledge-update turn, the system ({len(ku)} questions):")
        for k, v in Counter(r["update_turn_handling"] for r in ku).most_common():
            print(f"  {k:<24} {v:>3}  ({v / len(ku):.0%})")
    if ctrl:
        print(f"\nControl questions (no update in the data) where it still asked the user: "
              f"{summary['control_questions_with_any_conflict']}/{len(ctrl)}")
    print(f"\ntokens in/out: {u['prompt_tokens']:,}/{u['completion_tokens']:,}  "
          f"≈ ${summary['approx_cost_usd']}  ({llm.calls} calls, {summary['minutes']} min)")
    print(f"files: {out_dir}/qa_{tag}.csv, {out_dir}/summary_{tag}.json")
    print(f"every prompt + output: {llm.md_path}  (full JSON: {llm.jsonl_path})")


if __name__ == "__main__":
    main()
