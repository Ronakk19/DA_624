"""
Strategy + context tests with a scripted fake LLM (no Groq calls).
Reproduces: "I study at IITG" -> "I'm at IIT K" -> conflict -> resolve / delete.
"""

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database.schema import SCHEMA
from memory.store import MemoryStore
from memory.models import MemoryStatus
from strategies.baseline import BaselineStrategy


class ScriptedLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def json_completion(self, messages, **kw):
        return self.decisions.pop(0)


def _store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return MemoryStore(conn)


def test_conflict_is_flagged_not_overwritten():
    store = _store()
    llm = ScriptedLLM([{"action": "conflict", "target": 1, "confidence": 0.9,
                        "reason": "Different college", "options": ["User studies at IIT Kanpur."]}])
    strat = BaselineStrategy(store, llm=llm)
    strat.apply_candidates("c1", [{"content": "User studies at IIT Guwahati."}], "t1")
    events = strat.apply_candidates("c1", [{"content": "User studies at IIT Kanpur."}], "t2")

    assert events[0].type.value == "conflict"
    active = [m.content for m in store.list(status=MemoryStatus.ACTIVE)]
    assert active == ["User studies at IIT Guwahati."]           # untouched until the user decides
    assert len(store.events.pending_conflicts()) == 1

    strat.resolve_conflict(events[0].id, "User studies at IIT Kanpur.")
    active = [m.content for m in store.list(status=MemoryStatus.ACTIVE)]
    assert active == ["User studies at IIT Kanpur."]
    assert store.last_user_memory_change() is not None            # chat context gets cut here


def test_complementary_fact_is_added_not_overwritten():
    store = _store()
    llm = ScriptedLLM([{"action": "add", "target": None, "confidence": 0.8}])
    strat = BaselineStrategy(store, llm=llm)
    strat.apply_candidates("c1", [{"content": "User studies at IIT Guwahati."}], "t1")
    strat.apply_candidates("c1", [{"content": "User studies data science at IIT Guwahati."}], "t2")
    assert len(store.list(status=MemoryStatus.ACTIVE)) == 2


def test_deleted_memory_cuts_chat_context():
    from ui.chat import context_messages
    store = _store()
    conv = store.create_conversation()
    store.add_message(conv.id, "user", "I study at IITG")
    store.add_message(conv.id, "assistant", "Nice!")
    strat = BaselineStrategy(store, llm=ScriptedLLM([]))
    strat.apply_candidates(conv.id, [{"content": "User studies at IIT Guwahati."}], "t1")
    mem = store.list(status=MemoryStatus.ACTIVE)[0]

    assert len(context_messages(store, conv.id)) == 2
    time.sleep(1.1)  # timestamps have 1-second resolution
    store.delete(mem.id)
    time.sleep(1.1)
    store.add_message(conv.id, "user", "Where do I study?")

    sent = [m.content for m in context_messages(store, conv.id)]
    assert sent == ["Where do I study?"]                          # "I study at IITG" no longer leaks
    assert store.search("Where do I study?") == []                # and it's not retrievable


def test_retrieval_matches_study_and_studying():
    store = _store()
    strat = BaselineStrategy(store, llm=ScriptedLLM([{"action": "add"}] * 3))
    for c in ["User likes coffee.", "User is studying at IIT Kanpur.", "User prefers Java."]:
        strat.apply_candidates("c1", [{"content": c}], "t")
    top = store.search("Where do I study?", top_k=1)[0][0]
    assert "IIT Kanpur" in top.content
