"""
Core data models for the memory system.

These are plain dataclasses so they're cheap to construct, easy to serialize
to/from SQLite rows, and don't drag in a validation dependency the rest of
the app doesn't need.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional
import json
import uuid


def new_id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


class MemoryType(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    TASK = "task"
    GOAL = "goal"
    RELATIONSHIP = "relationship"
    OTHER = "other"


class MemoryScope(str, Enum):
    SESSION = "session"       # only relevant to the current chat session
    LONG_TERM = "long_term"   # persists across sessions
    TASK = "task"             # tied to a specific task/project


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    COMPRESSED = "compressed"   # merged into another memory
    DELETED = "deleted"
    TEMPORARY = "temporary"     # short-lived, may be auto-expired


class EventType(str, Enum):
    SAVED = "saved"
    UPDATED = "updated"
    CONFLICT = "conflict"
    COMPRESSED = "compressed"
    EDITED = "edited"
    DELETED = "deleted"
    RESTORED = "restored"


EVENT_ICON = {
    EventType.SAVED: "✓",
    EventType.UPDATED: "↻",
    EventType.CONFLICT: "⚠",
    EventType.COMPRESSED: "🗜",
    EventType.EDITED: "✎",
    EventType.DELETED: "🗑",
    EventType.RESTORED: "⟲",
}


@dataclass
class Memory:
    id: str = field(default_factory=lambda: new_id("mem"))
    content: str = ""
    type: MemoryType = MemoryType.FACT
    scope: MemoryScope = MemoryScope.LONG_TERM
    importance: float = 0.5          # 0..1, how important the memory is
    confidence: float = 0.8          # 0..1, how confident extraction was
    status: MemoryStatus = MemoryStatus.ACTIVE
    provenance: str = ""             # e.g. "Conversation #42"
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    conversation_id: Optional[str] = None

    def to_row(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, MemoryType) else self.type
        d["scope"] = self.scope.value if isinstance(self.scope, MemoryScope) else self.scope
        d["status"] = self.status.value if isinstance(self.status, MemoryStatus) else self.status
        return d

    @staticmethod
    def from_row(row: dict) -> "Memory":
        return Memory(
            id=row["id"],
            content=row["content"],
            type=MemoryType(row["type"]),
            scope=MemoryScope(row["scope"]),
            importance=row["importance"],
            confidence=row["confidence"],
            status=MemoryStatus(row["status"]),
            provenance=row.get("provenance") or "",
            version=row.get("version", 1),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            conversation_id=row.get("conversation_id"),
        )


@dataclass
class MemoryVersion:
    """A snapshot of a memory at a point in time, for version history."""
    id: str = field(default_factory=lambda: new_id("ver"))
    memory_id: str = ""
    content: str = ""
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    reason: str = ""


@dataclass
class MemoryEvent:
    id: str = field(default_factory=lambda: new_id("evt"))
    type: EventType = EventType.SAVED
    memory_id: Optional[str] = None
    before: str = ""
    after: str = ""
    reason: str = ""
    source: str = ""              # e.g. "Conversation #42"
    created_at: str = field(default_factory=now_iso)
    resolved: bool = True         # False for pending conflicts
    options: Optional[list] = None  # candidate resolutions, for conflicts

    def icon(self) -> str:
        return EVENT_ICON.get(self.type, "•")

    def to_row(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, EventType) else self.type
        d["options"] = json.dumps(self.options) if self.options is not None else None
        d["resolved"] = int(self.resolved)
        return d

    @staticmethod
    def from_row(row: dict) -> "MemoryEvent":
        return MemoryEvent(
            id=row["id"],
            type=EventType(row["type"]),
            memory_id=row.get("memory_id"),
            before=row.get("before") or "",
            after=row.get("after") or "",
            reason=row.get("reason") or "",
            source=row.get("source") or "",
            created_at=row["created_at"],
            resolved=bool(row.get("resolved", 1)),
            options=json.loads(row["options"]) if row.get("options") else None,
        )


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: new_id("conv"))
    title: str = "New conversation"
    created_at: str = field(default_factory=now_iso)


@dataclass
class ChatMessage:
    id: str = field(default_factory=lambda: new_id("msg"))
    conversation_id: str = ""
    role: str = "user"   # user | assistant
    content: str = ""
    created_at: str = field(default_factory=now_iso)
