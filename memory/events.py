"""
Persistence for memory events (the audit trail shown in the sidebar).

Kept separate from store.py so "what happened" (events) is clearly
distinct from "what is true right now" (memory state).
"""

from __future__ import annotations

from typing import List, Optional
import sqlite3

from database.schema import get_connection
from memory.models import MemoryEvent, EventType


class EventLog:
    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self.conn = conn or get_connection()

    def add(self, event: MemoryEvent) -> MemoryEvent:
        row = event.to_row()
        self.conn.execute(
            """INSERT INTO memory_events
               (id, type, memory_id, before, after, reason, source, created_at, resolved, options)
               VALUES (:id, :type, :memory_id, :before, :after, :reason, :source, :created_at, :resolved, :options)""",
            row,
        )
        self.conn.commit()
        return event

    def resolve(self, event_id: str) -> None:
        self.conn.execute(
            "UPDATE memory_events SET resolved = 1 WHERE id = ?", (event_id,)
        )
        self.conn.commit()

    def recent(self, limit: int = 25) -> List[MemoryEvent]:
        rows = self.conn.execute(
            "SELECT * FROM memory_events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [MemoryEvent.from_row(dict(r)) for r in rows]

    def pending_conflicts(self) -> List[MemoryEvent]:
        rows = self.conn.execute(
            "SELECT * FROM memory_events WHERE type = ? AND resolved = 0 ORDER BY created_at DESC",
            (EventType.CONFLICT.value,),
        ).fetchall()
        return [MemoryEvent.from_row(dict(r)) for r in rows]

    def get(self, event_id: str) -> Optional[MemoryEvent]:
        row = self.conn.execute(
            "SELECT * FROM memory_events WHERE id = ?", (event_id,)
        ).fetchone()
        return MemoryEvent.from_row(dict(row)) if row else None

    def counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT type, COUNT(*) as c FROM memory_events GROUP BY type"
        ).fetchall()
        return {r["type"]: r["c"] for r in rows}
