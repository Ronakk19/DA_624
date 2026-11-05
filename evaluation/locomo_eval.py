"""
LoCoMo evaluation for the memory pipeline, with error attribution.

What it does, for one LoCoMo conversation:
  1. INGEST   - feed the conversation session by session (in chunks of turns)
                through the SAME save/update/conflict logic the app uses
                (BaselineStrategy.apply_candidates), into a fresh in-memory store.
  2. ANSWER   - for each benchmark question: retrieve top-k memories, ask the LLM
                to answer from those memories only.
  3. SCORE    - token-level F1 (LoCoMo's metric) + an LLM judge (correct / wrong).
  4. ATTRIBUTE every wrong answer to the stage that failed:
       reasoning   : the needed info WAS in the retrieved memories, LLM still got it wrong
       retrieval   : info is in the active store but was NOT retrieved
       management  : info only survives in an OLD version (it was overwritten)
                     -> detectable only because the store keeps version history
       extraction  : info never made it into memory at all

Usage (from the project folder):
    python -m evaluation.locomo_eval --data path/to/locomo10.json --conv 0 --max-questions 60
    python -m evaluation.locomo_eval --data ... --policy relation       # ablation
    python -m evaluation.locomo_eval --data ... --policy overwrite      # ablation: original behaviour
    python -m evaluation.locomo_eval --data ... --dry-run               # no API calls, checks the pipeline

LoCoMo: https://github.com/snap-research/locomo  (data/locomo10.json)
Category ids in the data: 1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop,
5=adversarial (no gold answer; skipped by default, as in the Mem0 paper).

Groq free tier note: llama-3.1-8b-instant has far higher daily limits than the
70B model, so it is the default here. One conversation with --max-questions 60
is roughly 150-300 API calls.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sqlite3
import string
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from database.schema import SCHEMA
from memory.store import MemoryStore
from memory.models import MemoryStatus
from strategies.baseline import BaselineStrategy

CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}

# --------------------------------------------------------------------- prompts

EXTRACT_PROMPT = """You extract durable facts from a chat between two people, {a} and {b}.
The chat took place on: {date}.

Extract every fact worth remembering long-term about EITHER person: events, plans,
preferences, relationships, possessions, jobs, hobbies, feelings about things.
Rules:
- Always use the person's NAME (never "the user", "he", "she").
- Convert relative times to absolute dates using the chat date
  (e.g. "yesterday" on 8 May 2023 -> "on 7 May 2023"; "last year" -> "in 2022").
- One fact per item, short and self-contained.

Respond ONLY with JSON:
{{"memories": [{{"content": "...", "type": "fact"|"preference"|"task"|"goal"|"relationship"|"other",
"scope": "long_term", "importance": 0-1, "confidence": 0-1}}]}}
If nothing is worth remembering return {{"memories": []}}."""

ANSWER_PROMPT = """Answer the question using ONLY the memories below.
Give a short answer (a few words or a date). If the memories do not contain the answer,
make your best guess from them.

Memories:
{memories}

Question: {question}
Short answer:"""

JUDGE_PROMPT = """You grade a memory system's answer against a gold answer.
Question: {question}
Gold answer: {gold}
System answer: {pred}
Be generous with wording and date formats: if the system answer refers to the same
thing / same time period as the gold answer, it is correct.
Respond ONLY with JSON: {{"correct": true|false}}"""

CONTAINS_PROMPT = """Below is a list of memories and a question with its gold answer.
Do the memories contain the information needed to arrive at the gold answer?
(It may need combining a few memories; exact wording does not matter.)

Memories:
{memories}

Question: {question}
Gold answer: {gold}
Respond ONLY with JSON: {{"contains": true|false}}"""


# ------------------------------------------------------------------ LLM wrapper

class EvalLLM:
    """Wraps LLMClient: fixed model, retries with backoff on rate limits, call counting."""

    def __init__(self, model: str, sleep: float = 0.0):
        from llm.client import LLMClient
        self.inner = LLMClient()
        self.model = model
        self.sleep = sleep
        self.calls = 0

    def _retry(self, fn, *args, **kwargs):
        delay = 5.0
        for attempt in range(8):
            try:
                self.calls += 1
                out = fn(*args, model=self.model, **kwargs)
                if self.sleep:
                    time.sleep(self.sleep)
                return out
            except Exception as e:  # rate limit / transient
                msg = str(e).lower()
                if "rate" in msg or "429" in msg or "timeout" in msg or "503" in msg or "overloaded" in msg:
                    print(f"    [rate limited, sleeping {delay:.0f}s]")
                    time.sleep(delay)
                    delay = min(delay * 2, 120)
                    continue
                raise
        raise RuntimeError("Too many retries against the LLM API")

    def chat(self, messages, **kw):
        kw.pop("model", None)
        return self._retry(self.inner.chat, messages, **kw)

    def json_completion(self, messages, **kw):
        kw.pop("model", None)
        return self._retry(self.inner.json_completion, messages, **kw)


class FakeLLM:
    """--dry-run: deterministic stand-in so the whole pipeline can be checked offline."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return "unknown"

    def json_completion(self, messages, **kw):
        self.calls += 1
        text = messages[-1]["content"]
        if messages[0]["content"].startswith("You extract durable facts from a chat"):
            lines = [l for l in text.splitlines() if ":" in l][:3]
            return {"memories": [{"content": l.split("]", 1)[-1].strip(), "type": "fact"} for l in lines]}
        if "EXISTING memories:" in text:
            return {"action": random.choice(["add", "add", "update", "skip", "conflict"]),
                    "target": 1, "confidence": 0.9, "options": ["a", "b"]}
        if "EXISTING memory" in text:
            return {"conflict": False, "relation": "complementary", "confidence": 0.1}
        if "Gold answer" in text and "System answer" in text:
            return {"correct": False}
        return {"contains": random.random() < 0.5}


# ------------------------------------------------------------------- helpers

def fresh_store() -> MemoryStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return MemoryStore(conn)


def sessions_of(conv: dict):
    """Yield (session_number, date_string, turns) in order."""
    nums = sorted(int(m.group(1)) for k in conv
                  if (m := re.fullmatch(r"session_(\d+)", k)) and isinstance(conv[k], list))
    for n in nums:
        yield n, conv.get(f"session_{n}_date_time", "unknown date"), conv[f"session_{n}"]


def turn_text(t: dict) -> str:
    s = f"[{t.get('dia_id', '')}] {t['speaker']}: {t.get('text', '')}"
    if t.get("blip_caption"):  # LoCoMo shares images; the caption stands in for the image
        s += f" (shared an image: {t['blip_caption']})"
    return s


def normalize(s: str) -> str:
    s = str(s).lower().replace(",", "")
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = re.sub(r"\b(a|an|the|and)\b", " ", s)
    return " ".join(s.split())


def token_f1(pred: str, gold: str) -> float:
    p, g = normalize(pred).split(), normalize(gold).split()
    if not p or not g:
        return float(p == g)
    common = Counter(p) & Counter(g)
    same = sum(common.values())
    if same == 0:
        return 0.0
    prec, rec = same / len(p), same / len(g)
    return 2 * prec * rec / (prec + rec)


def as_bullets(items, limit_chars: int = 24000) -> str:
    out, total = [], 0
    for it in items:
        line = f"- {it}"
        total += len(line)
        if total > limit_chars:
            break
        out.append(line)
    return "\n".join(out) if out else "(no memories)"


# ---------------------------------------------------------------------- main

def ingest(conv: dict, strategy: BaselineStrategy, llm, chunk: int, auto_resolve):
    a, b = conv["speaker_a"], conv["speaker_b"]
    conv_id = "locomo"
    for n, date, turns in sessions_of(conv):
        n_before = len(strategy.store.list(status=MemoryStatus.ACTIVE))
        for i in range(0, len(turns), chunk):
            block = turns[i:i + chunk]
            ids = f"{block[0].get('dia_id')}..{block[-1].get('dia_id')}"
            msgs = [{"role": "system", "content": EXTRACT_PROMPT.format(a=a, b=b, date=date)},
                    {"role": "user", "content": "\n".join(turn_text(t) for t in block)}]
            try:
                out = llm.json_completion(msgs)
            except Exception as e:
                print(f"    extraction failed on {ids}: {e}")
                continue
            cands = out.get("memories", []) if isinstance(out, dict) else []
            strategy.apply_candidates(conv_id, cands, source=f"session {n} ({date}) {ids}",
                                      auto_resolve=auto_resolve)
        n_after = len(strategy.store.list(status=MemoryStatus.ACTIVE))
        print(f"  session {n:>2} [{date}]: {len(turns)} turns -> active memories {n_before} -> {n_after}")


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="path to locomo10.json")
    ap.add_argument("--conv", type=int, default=0, help="which of the 10 conversations (0-9)")
    ap.add_argument("--max-questions", type=int, default=60, help="random sample of questions (0 = all)")
    ap.add_argument("--categories", default="1,2,3,4")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--chunk", type=int, default=8, help="turns per extraction call")
    ap.add_argument("--policy", choices=["decision", "relation", "overwrite"], default="decision",
                    help="memory management policy (see strategies/baseline.py)")
    ap.add_argument("--conflicts", choices=["pending", "accept-new"], default="pending",
                    help="pending = leave for the human (old memory kept); accept-new = auto-accept")
    ap.add_argument("--model", default=os.environ.get("EVAL_MODEL", "llama-3.1-8b-instant"))
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to wait after each API call")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="results")
    ap.add_argument("--no-embeddings", action="store_true", help="force TF-IDF retrieval")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    random.seed(args.seed)
    if args.no_embeddings:
        os.environ["MEMORY_EMBEDDINGS"] = "off"
    from memory.store import retrieval_backend
    data = json.load(open(args.data))
    sample = data[args.conv]
    conv = sample["conversation"]
    cats = {int(c) for c in args.categories.split(",")}

    llm = FakeLLM() if args.dry_run else EvalLLM(args.model, args.sleep)
    store = fresh_store()
    strategy = BaselineStrategy(store, llm=llm, non_conflict_policy=args.policy)
    auto_resolve = "new" if args.conflicts == "accept-new" else None

    tag = f"conv{args.conv}_{args.policy}_{args.conflicts}" + ("_dry" if args.dry_run else "")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"== LoCoMo {sample.get('sample_id', args.conv)}: {conv['speaker_a']} & {conv['speaker_b']}  "
          f"| model={getattr(llm, 'model', 'fake')} policy={args.policy} conflicts={args.conflicts} "
          f"retrieval={retrieval_backend()}")

    # 1. ingest ------------------------------------------------------------
    t0 = time.time()
    ingest(conv, strategy, llm, args.chunk, auto_resolve)
    ingest_calls = llm.calls
    active = store.list(status=MemoryStatus.ACTIVE)
    ev = store.events.counts()
    print(f"  ingest done: {len(active)} active memories, events={ev}, "
          f"{ingest_calls} LLM calls, {time.time() - t0:.0f}s")

    # all historical versions (to detect facts lost by overwrite)
    old_versions = [dict(r)["content"] for r in store.conn.execute(
        "SELECT v.content FROM memory_versions v JOIN memories m ON m.id = v.memory_id "
        "WHERE v.version < m.version OR m.status != 'active'").fetchall()]

    # 2-4. answer, score, attribute ------------------------------------------
    qas = [q for q in sample["qa"] if q.get("category") in cats and "answer" in q]
    if args.max_questions and len(qas) > args.max_questions:
        qas = random.sample(qas, args.max_questions)

    rows = []
    for i, q in enumerate(qas, 1):
        question, gold = q["question"], str(q["answer"])
        retrieved = strategy.retrieve_for_query(question, top_k=args.top_k)
        ret_text = [m.content for m, _ in retrieved]

        pred = llm.chat([{"role": "user", "content": ANSWER_PROMPT.format(
            memories=as_bullets(ret_text), question=question)}], temperature=0.0, max_tokens=400)
        pred = (pred or "").strip().splitlines()[0] if pred else ""
        f1 = token_f1(pred, gold)
        judge = llm.json_completion([{"role": "user", "content": JUDGE_PROMPT.format(
            question=question, gold=gold, pred=pred)}])
        correct = bool(judge.get("correct")) if isinstance(judge, dict) else False

        def contains(mems):
            if not mems:
                return False
            r = llm.json_completion([{"role": "user", "content": CONTAINS_PROMPT.format(
                memories=as_bullets(mems), question=question, gold=gold)}])
            return bool(r.get("contains")) if isinstance(r, dict) else False

        in_retrieved = contains(ret_text)
        if correct:
            failure = ""
        elif in_retrieved:
            failure = "reasoning"
        elif contains([m.content for m in active]):
            failure = "retrieval"
        elif contains(old_versions):
            failure = "management"
        else:
            failure = "extraction"

        rows.append({
            "category": CATEGORY_NAMES.get(q["category"], q["category"]),
            "question": question, "gold": gold, "pred": pred,
            "f1": round(f1, 3), "judge_correct": int(correct),
            "info_in_retrieved": int(in_retrieved), "failure_stage": failure,
            "evidence": " ".join(q.get("evidence", [])),
            "retrieved": " || ".join(ret_text),
        })
        mark = "✓" if correct else f"✗ {failure}"
        print(f"  [{i:>3}/{len(qas)}] {mark:<14} {question[:70]}")

    # ---------------------------------------------------------------- report
    with open(out_dir / f"qa_{tag}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["question"])
        w.writeheader()
        w.writerows(rows)
    with open(out_dir / f"memories_{tag}.json", "w", encoding="utf-8") as f:
        json.dump([{"content": m.content, "type": m.type.value, "version": m.version,
                    "provenance": m.provenance} for m in active], f, indent=1, ensure_ascii=False)

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    wrong = [r for r in rows if not r["judge_correct"]]
    summary = {
        "conversation": sample.get("sample_id", args.conv),
        "model": getattr(llm, "model", "fake"), "policy": args.policy, "retrieval": retrieval_backend(), "conflicts": args.conflicts,
        "top_k": args.top_k, "n_questions": len(rows),
        "active_memories": len(active), "events": ev,
        "pending_conflicts": len(store.events.pending_conflicts()),
        "llm_judge_accuracy": round(sum(r["judge_correct"] for r in rows) / max(len(rows), 1), 3),
        "mean_f1": round(sum(r["f1"] for r in rows) / max(len(rows), 1), 3),
        "by_category": {c: {"n": len(v),
                            "judge_acc": round(sum(r["judge_correct"] for r in v) / len(v), 3),
                            "f1": round(sum(r["f1"] for r in v) / len(v), 3)}
                        for c, v in sorted(by_cat.items())},
        "failure_breakdown": dict(Counter(r["failure_stage"] for r in wrong)),
        "llm_calls": llm.calls,
    }
    with open(out_dir / f"summary_{tag}.json", "w") as f:
        json.dump(summary, f, indent=2)

    print("\n================ SUMMARY ================")
    print(f"questions: {len(rows)} | LLM-judge accuracy: {summary['llm_judge_accuracy']:.1%} "
          f"| mean F1: {summary['mean_f1']:.3f}")
    print(f"{'category':<13}{'n':>4}{'judge acc':>11}{'F1':>8}")
    for c, s in summary["by_category"].items():
        print(f"{c:<13}{s['n']:>4}{s['judge_acc']:>11.1%}{s['f1']:>8.3f}")
    print(f"\nwhy the {len(wrong)} wrong answers failed:")
    for stage in ["extraction", "management", "retrieval", "reasoning"]:
        n = summary["failure_breakdown"].get(stage, 0)
        print(f"  {stage:<11} {n:>3}  ({n / max(len(wrong), 1):.0%})")
    print(f"\nfiles written to {out_dir}/  (tag: {tag})")


if __name__ == "__main__":
    main()
