"""
SQLite schema definition and connection helper.

SQLite is used because it's zero-setup, file-backed, and plenty for a
single-user memory/chat app. Everything persists in one .db file so the
app survives restarts and Streamlit reruns.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "app_data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    type TEXT NOT NULL,
    scope TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.8,
    status TEXT NOT NULL DEFAULT 'active',
    provenance TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    conversation_id TEXT
);

CREATE TABLE IF NOT EXISTS memory_versions (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT,
    FOREIGN KEY (memory_id) REFERENCES memories(id)
);

CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    memory_id TEXT,
    before TEXT,
    after TEXT,
    reason TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 1,
    options TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS retrievals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    query TEXT NOT NULL,
    retrieved_json TEXT NOT NULL,
    final_prompt TEXT,
    token_estimate INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_events_created ON memory_events(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
