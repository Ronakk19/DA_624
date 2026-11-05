"""
Scripted evaluation runner.

Feeds a fixed transcript of (user, assistant) turns through a strategy and
reports what got extracted/updated/flagged, so you can compare strategies
or catch regressions without going through the UI. Run directly:

    python -m evaluation.runner
"""

from __future__ import annotations

from typing import List, Tuple

from database.schema import init_db, get_connection
from memory.store import MemoryStore
from strategies.baseline import BaselineStrategy

# A small fixed transcript for smoke-testing extraction + conflict handling.
SAMPLE_TRANSCRIPT: List[Tuple[str, str]] = [
    ("I'm learning NLP right now, working through transformer architectures.",
     "Great! Transformers are a great place to focus -- attention mechanisms especially."),
    ("Actually I finished the transformers section, moving on to fine-tuning now.",
     "Nice progress! Fine-tuning is a natural next step."),
    ("I prefer Java over Python generally, but I've been using Python for this ML work.",
     "Makes sense -- Python's ecosystem is hard to beat for ML."),
]


def run(strategy: BaselineStrategy, store: MemoryStore) -> None:
    conv = store.create_conversation(title="Evaluation run")
    for user_msg, assistant_msg in SAMPLE_TRANSCRIPT:
        store.add_message(conv.id, "user", user_msg)
        store.add_message(conv.id, "assistant", assistant_msg)
        events = strategy.process_turn(conv.id, user_msg, assistant_msg)
        print(f"\nTurn: {user_msg!r}")
        for e in events:
            print(f"  [{e.type.value}] {e.after or e.before}")

    print("\n--- Final memory state ---")
    for m in store.list():
        print(f"  ({m.status.value}) {m.content}  [type={m.type.value}, scope={m.scope.value}]")


if __name__ == "__main__":
    init_db()
    conn = get_connection()
    store = MemoryStore(conn)
    strategy = BaselineStrategy(store)
    run(strategy, store)
