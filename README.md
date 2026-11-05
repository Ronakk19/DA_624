# When Should a Memory System Ask the User?
### A transparent, human-controllable memory layer for LLM chatbots

**Prem Prakash Agarwal (264161005)** · M.Tech Data Science, IIT Guwahati

A Streamlit chat app whose long-term memory is visible and controllable. Every memory the
assistant saves, updates or flags appears in a live event feed with its reason and source.
You can inspect, edit, delete, restore and trace memories through their version history,
and the app **asks you only when new information seems to contradict what it knows**.
The project evaluates *when* the system asks, and whether those questions are worth asking,
on the LongMemEval benchmark.

| | |
|---|---|
| **Notebook (demo + all results)** | [`demo_and_results.ipynb`](demo_and_results.ipynb) |
| **Report** | `report/report.pdf` (ACL format) |
| **Raw results** | [`results/`](results): per-question CSV, summary JSON, full trace of every prompt and output |

---

## 1. Setup

Requires Python 3.10+.

```bash
git clone <this-repo-url>
cd memory-chat
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add a Groq API key (free at https://console.groq.com/keys):

```
GROQ_API_KEY=gsk_...
GROQ_CHAT_MODEL=openai/gpt-oss-20b
GROQ_JSON_MODEL=openai/gpt-oss-20b
EVAL_MODEL=openai/gpt-oss-20b
REASONING_EFFORT=low
```

Optional, for semantic retrieval: `pip install sentence-transformers`. Without it, retrieval
uses word + character n-gram TF-IDF.

## 2. Run the app

```bash
streamlit run app.py
```

- **Chat** (main panel): a normal chat. After every turn, memories are extracted and managed.
- **Sidebar**: live memory events (✓ saved, ↻ updated, ⚠ conflict, ✎ edited, 🗑 deleted, ⟲ restored),
  each expandable to show before/after, reason and source. Pending conflicts appear as cards
  with suggested resolutions, plus *keep existing*, *don't store* and *custom*. You can also add memories by hand.
- **Memory Explorer** page: filter and search all memories, see full details and version history,
  and edit, delete or restore them.
- **Evaluation** page: retrieval inspector for the last turn (query, retrieved memories with scores,
  final prompt, token estimate) plus memory statistics.
- **Settings** page: model and thresholds.

Try this: say *"I study at IIT Guwahati"*, then *"I'm at IIT Kanpur now"*. Watch the event feed or
the conflict card, then delete a memory and ask about it. A 🔒 divider shows where the chat context
was cut, so the deleted fact can't leak back in.

## 3. Reproduce the evaluation

```bash
# 1. dataset (LongMemEval, MIT licence), oracle version only
pip install -U huggingface_hub
hf download xiaowu0162/longmemeval-cleaned longmemeval_oracle.json --repo-type=dataset --local-dir .

# 2. offline check (no API calls)
python -m evaluation.longmemeval_eval --data longmemeval_oracle.json --per-type 2 --dry-run

# 3. the run reported in the paper: 15 knowledge-update + 15 single-session-user, seed 0
python -m evaluation.longmemeval_eval --data longmemeval_oracle.json --per-type 15
```

The run takes about 7 minutes and costs about US$0.05 with `openai/gpt-oss-20b` on Groq.
Outputs go to `results/`:

| file | contents |
|---|---|
| `summary_lme_decision.json` | accuracy by type, update handling, questions asked, tokens, cost |
| `qa_lme_decision.csv` | every question: gold answer, answers with and without the human, conflicts, retrieved memories |
| `trace_lme_decision.md` / `.jsonl` | **every prompt sent to the LLM and every output**, grouped by question and turn |

Then open `demo_and_results.ipynb` to regenerate all tables and figures.

**How the evaluation works.** For each question, a fresh memory is built by **replaying** the
recorded user–assistant chat turn by turn through the app's own pipeline. Nothing is generated
mid-chat, so the conversation can't drift. The question is then answered from memory only:
(a) with no human, where pending conflicts keep the old value, and (b) after a simulated user accepts
every pending conflict. Answers are judged with the **official LongMemEval judge prompts**. For
knowledge-update questions, the script also records what the system did at the turn where the
user's information changed: asked the user, made a silent update, kept both, or didn't catch it.

## 4. Tests

```bash
pytest tests/
```

The tests use a scripted fake LLM, so no API key is needed. They cover store CRUD and versioning,
conflict handling, the context cut-off after a deletion, and retrieval.

## 5. Results (main run, 30 questions)

| | Knowledge-update (15) | Single-session-user (15) |
|---|---|---|
| Correct, no human | **87%** | **80%** |
| Correct, simulated user (accept new) | 80% | 80% |
| Questions asked to the user | 4 | 0 |

- The system asks **rarely** and never on plain recall, but only **1 of its 4 questions was worth
  asking**. That one caught a value the system had invented itself.
- Clear changes handled as **silent updates that record the previous value**: 8/8 correct.
- A user who blindly accepts new information makes answers **worse**.

## 6. Project structure

```
app.py                     Streamlit entry point
pages/                     memory_explorer.py, evaluation.py (retrieval inspector), settings.py
ui/                        chat.py, memory_sidebar.py, conflict_dialog.py, memory_details.py, retrieval_inspector.py
memory/                    models.py, store.py (SQLite store, versions, search), events.py, base.py
strategies/                base.py (strategy interface), baseline.py (extract -> add/update/skip/conflict)
llm/                       client.py (Groq client), prompts.py (all prompts)
evaluation/                longmemeval_eval.py (main evaluation), locomo_eval.py (earlier LoCoMo harness), metrics.py
database/                  schema.py (SQLite schema; app_data.db is created at first run)
tests/                     unit tests (no API key needed)
results/                   raw results of the reported run
demo_and_results.ipynb     demo + reproduction of every table and figure
```

## References

- Wu et al. *LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory.* ICLR 2025.
- Chhikara et al. *Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory.* 2025.
- Zhong et al. *MemoryBank.* AAAI 2024 · Xu et al. *A-MEM.* · Maharana et al. *LoCoMo.* ACL 2024.
