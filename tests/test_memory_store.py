"""
Basic smoke tests for MemoryStore, independent of the LLM (no Groq calls).
Run with: pytest
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database.schema import SCHEMA
from memory.store import MemoryStore
from memory.models import Memory, MemoryStatus, MemoryType, MemoryScope


def _fresh_store() -> MemoryStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return MemoryStore(conn)


def test_create_and_get():
    store = _fresh_store()
    mem = Memory(content="User is studying NLP.", type=MemoryType.FACT, scope=MemoryScope.LONG_TERM)
    store.create(mem)
    fetched = store.get(mem.id)
    assert fetched is not None
    assert fetched.content == "User is studying NLP."
    assert fetched.status == MemoryStatus.ACTIVE


def test_update_creates_version_and_event():
    store = _fresh_store()
    mem = Memory(content="User is studying transformers.")
    store.create(mem)
    store.update(mem.id, "User has completed transformers.", reason="topic finished")

    updated = store.get(mem.id)
    assert updated.content == "User has completed transformers."
    assert updated.version == 2

    history = store.version_history(mem.id)
    assert len(history) == 2

    events = store.events.recent(limit=10)
    assert any(e.memory_id == mem.id and e.type.value == "updated" for e in events)


def test_delete_and_restore():
    store = _fresh_store()
    mem = Memory(content="Temporary note.")
    store.create(mem)
    store.delete(mem.id)
    assert store.get(mem.id).status == MemoryStatus.DELETED

    store.restore(mem.id)
    assert store.get(mem.id).status == MemoryStatus.ACTIVE


def test_search_returns_relevant_memory():
    store = _fresh_store()
    store.create(Memory(content="User is studying natural language processing."))
    store.create(Memory(content="User enjoys hiking on weekends."))

    results = store.search("What is the user studying?", top_k=2)
    assert len(results) > 0
    top_memory, score = results[0]
    assert "studying" in top_memory.content or "language" in top_memory.content


def test_conversation_and_messages():
    store = _fresh_store()
    conv = store.create_conversation("Test convo")
    store.add_message(conv.id, "user", "Hello")
    store.add_message(conv.id, "assistant", "Hi there!")

    messages = store.get_messages(conv.id)
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"
