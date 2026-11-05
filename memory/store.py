"""
Concrete MemoryStore backed by SQLite.

This is the single source of truth for persistent state: memories, their
version history, memory events, conversations, chat messages, and logged
retrievals (for the debug/inspector page). Streamlit session_state should
never hold this data directly -- it should always be read fresh from here.
"""

from __future__ import annotations

from typing import List, Optional
import json
import sqlite3

from database.schema import get_connection
from memory.base import BaseMemoryStore
from memory.models import (
    Memory, MemoryVersion, MemoryEvent, EventType, MemoryStatus,
    Conversation, ChatMessage, now_iso, new_id,
)
from memory.events import EventLog

import os

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    _HAS_SKLEARN = True
except ImportError:  # pragma: no cover - fallback path
    _HAS_SKLEARN = False

# ---- optional semantic embeddings -------------------------------------------
# If `sentence-transformers` is installed, retrieval uses real embeddings
# (understands "where do I study?" ~ "User is a student at IIT Kanpur").
# Otherwise it falls back to word + character n-gram TF-IDF, which at least
# matches "study" with "studying" (plain word TF-IDF scores that pair as 0).
# Set MEMORY_EMBEDDINGS=off to force the TF-IDF fallback.
_EMBEDDER = None          # None = not tried yet, False = unavailable
_EMB_CACHE: dict = {}


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        if os.environ.get("MEMORY_EMBEDDINGS", "auto").lower() == "off":
            _EMBEDDER = False
        else:
            try:
                from sentence_transformers import SentenceTransformer
                _EMBEDDER = SentenceTransformer(os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2"))
            except Exception:
                _EMBEDDER = False
    return _EMBEDDER or None


def retrieval_backend() -> str:
    if _get_embedder() is not None:
        return "embeddings (" + os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2") + ")"
    return "TF-IDF word + char n-grams" if _HAS_SKLEARN else "keyword overlap"


class MemoryStore(BaseMemoryStore):
    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self.conn = conn or get_connection()
        self.events = EventLog(self.conn)

    # ---------------------------------------------------------------- CRUD

    def create(self, memory: Memory, reason: str = "New information extracted", source: str = "") -> Memory:
        row = memory.to_row()
        self.conn.execute(
            """INSERT INTO memories
               (id, content, type, scope, importance, confidence, status,
                provenance, version, created_at, updated_at, conversation_id)
               VALUES (:id, :content, :type, :scope, :importance, :confidence, :status,
                       :provenance, :version, :created_at, :updated_at, :conversation_id)""",
            row,
        )
        self._add_version(memory.id, memory.content, memory.version, reason)
        self.conn.commit()
        self.events.add(MemoryEvent(
            type=EventType.SAVED, memory_id=memory.id,
            after=memory.content, reason=reason, source=source,
        ))
        return memory

    def update(self, memory_id: str, new_content: str, reason: str = "", source: str = "") -> Memory:
        existing = self.get(memory_id)
        if existing is None:
            raise ValueError(f"Memory {memory_id} not found")
        before = existing.content
        new_version = existing.version + 1
        updated_at = now_iso()
        self.conn.execute(
            "UPDATE memories SET content = ?, version = ?, updated_at = ? WHERE id = ?",
            (new_content, new_version, updated_at, memory_id),
        )
        self._add_version(memory_id, new_content, new_version, reason)
        self.conn.commit()
        self.events.add(MemoryEvent(
            type=EventType.UPDATED, memory_id=memory_id,
            before=before, after=new_content, reason=reason, source=source,
        ))
        existing.content = new_content
        existing.version = new_version
        existing.updated_at = updated_at
        return existing

    def edit(self, memory_id: str, new_content: str, reason: str = "Manually edited by user") -> Memory:
        """User-initiated edit (distinct event type from an auto UPDATED)."""
        existing = self.get(memory_id)
        if existing is None:
            raise ValueError(f"Memory {memory_id} not found")
        before = existing.content
        new_version = existing.version + 1
        updated_at = now_iso()
        self.conn.execute(
            "UPDATE memories SET content = ?, version = ?, updated_at = ? WHERE id = ?",
            (new_content, new_version, updated_at, memory_id),
        )
        self._add_version(memory_id, new_content, new_version, reason)
        self.conn.commit()
        self.events.add(MemoryEvent(
            type=EventType.EDITED, memory_id=memory_id,
            before=before, after=new_content, reason=reason, source="user",
        ))
        existing.content = new_content
        existing.version = new_version
        existing.updated_at = updated_at
        return existing

    def get(self, memory_id: str) -> Optional[Memory]:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return Memory.from_row(dict(row)) if row else None

    def list(self, status: Optional[MemoryStatus] = None, scope: Optional[str] = None) -> List[Memory]:
        query = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if status is not None:
            query += " AND status = ?"
            params.append(status.value if isinstance(status, MemoryStatus) else status)
        if scope is not None:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY updated_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [Memory.from_row(dict(r)) for r in rows]

    def delete(self, memory_id: str, reason: str = "Deleted by user") -> None:
        existing = self.get(memory_id)
        if existing is None:
            return
        self.conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
            (MemoryStatus.DELETED.value, now_iso(), memory_id),
        )
        self.conn.commit()
        self.events.add(MemoryEvent(
            type=EventType.DELETED, memory_id=memory_id,
            before=existing.content, reason=reason, source="user",
        ))

    def restore(self, memory_id: str) -> None:
        self.conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
            (MemoryStatus.ACTIVE.value, now_iso(), memory_id),
        )
        self.conn.commit()
        existing = self.get(memory_id)
        self.events.add(MemoryEvent(
            type=EventType.RESTORED, memory_id=memory_id,
            after=existing.content if existing else "", source="user",
        ))

    def compress(self, memory_ids: List[str], summary_content: str, reason: str = "Compressed for brevity") -> Memory:
        """Merge several memories into one new summary memory."""
        merged = Memory(content=summary_content, importance=0.6, confidence=0.9,
                         provenance="Compression: " + ", ".join(memory_ids))
        self.create(merged, reason=reason, source="compression")
        for mid in memory_ids:
            self.conn.execute(
                "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
                (MemoryStatus.COMPRESSED.value, now_iso(), mid),
            )
        self.conn.commit()
        self.events.add(MemoryEvent(
            type=EventType.COMPRESSED, memory_id=merged.id,
            before=f"{len(memory_ids)} memories", after=summary_content, reason=reason,
        ))
        return merged

    # ------------------------------------------------------------ versions

    def _add_version(self, memory_id: str, content: str, version: int, reason: str) -> None:
        v = MemoryVersion(memory_id=memory_id, content=content, version=version, reason=reason)
        self.conn.execute(
            "INSERT INTO memory_versions (id, memory_id, content, version, created_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (v.id, v.memory_id, v.content, v.version, v.created_at, v.reason),
        )
        self.conn.commit()

    def version_history(self, memory_id: str) -> List[MemoryVersion]:
        rows = self.conn.execute(
            "SELECT * FROM memory_versions WHERE memory_id = ? ORDER BY version ASC", (memory_id,)
        ).fetchall()
        return [MemoryVersion(**dict(r)) for r in rows]

    # ------------------------------------------------------------ counts

    def counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as c FROM memories GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # --------------------------------------------------------- retrieval

    def search(self, query: str, top_k: int = 5, status: MemoryStatus = MemoryStatus.ACTIVE) -> List[tuple]:
        """
        Return [(Memory, score), ...] sorted by relevance to `query`.

        Uses TF-IDF cosine similarity when scikit-learn is available (cheap,
        no external API call needed), falling back to a simple keyword
        overlap score otherwise so retrieval still works with a minimal
        install.
        """
        candidates = self.list(status=status)
        if not candidates:
            return []

        texts = [m.content for m in candidates]
        sims = self._similarities(query, texts)
        scored = list(zip(candidates, sims))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _similarities(self, query: str, texts: List[str]) -> List[float]:
        embedder = _get_embedder()
        if embedder is not None:
            try:
                missing = [t for t in texts if t not in _EMB_CACHE]
                if missing:
                    for t, v in zip(missing, embedder.encode(missing, normalize_embeddings=True)):
                        _EMB_CACHE[t] = v
                q = embedder.encode([query], normalize_embeddings=True)[0]
                return [float((_EMB_CACHE[t] * q).sum()) for t in texts]
            except Exception:
                pass  # fall through to TF-IDF
        if _HAS_SKLEARN:
            sims = None
            for vec in (TfidfVectorizer(stop_words="english"),
                        TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)):
                try:
                    m = vec.fit_transform(texts + [query])
                    part = cosine_similarity(m[-1], m[:-1])[0]
                except ValueError:  # empty vocabulary
                    part = [0.0] * len(texts)
                sims = list(part) if sims is None else [a + b for a, b in zip(sims, part)]
            return [float(x) / 2 for x in sims]
        return [self._keyword_overlap(query, t) for t in texts]

    def last_user_memory_change(self) -> Optional[str]:
        """
        Timestamp of the most recent time the USER changed memory (edit, delete,
        restore, or resolving a conflict). Chat messages from before this point
        are no longer sent to the LLM, otherwise a deleted fact would still
        leak back in through the raw chat history.
        """
        row = self.conn.execute(
            "SELECT MAX(created_at) AS t FROM memory_events "
            "WHERE type IN ('edited', 'deleted', 'restored') OR reason = 'Conflict resolved'"
        ).fetchone()
        return row["t"] if row and row["t"] else None

    @staticmethod
    def _keyword_overlap(query: str, content: str) -> float:
        q = set(query.lower().split())
        c = set(content.lower().split())
        if not q or not c:
            return 0.0
        return len(q & c) / len(q | c)

    def log_retrieval(self, conversation_id: str, query: str, retrieved: List[tuple],
                       final_prompt: str, token_estimate: int) -> None:
        payload = [{"memory_id": m.id, "content": m.content, "score": round(float(s), 4)}
                   for m, s in retrieved]
        self.conn.execute(
            """INSERT INTO retrievals (id, conversation_id, query, retrieved_json, final_prompt, token_estimate, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (new_id("ret"), conversation_id, query, json.dumps(payload), final_prompt, token_estimate, now_iso()),
        )
        self.conn.commit()

    def last_retrieval(self, conversation_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM retrievals WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["retrieved"] = json.loads(d["retrieved_json"])
        return d

    # ----------------------------------------------------------- convos

    def create_conversation(self, title: str = "New conversation") -> Conversation:
        c = Conversation(title=title)
        self.conn.execute(
            "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
            (c.id, c.title, c.created_at),
        )
        self.conn.commit()
        return c

    def list_conversations(self) -> List[Conversation]:
        rows = self.conn.execute("SELECT * FROM conversations ORDER BY created_at DESC").fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def add_message(self, conversation_id: str, role: str, content: str) -> ChatMessage:
        m = ChatMessage(conversation_id=conversation_id, role=role, content=content)
        self.conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (m.id, m.conversation_id, m.role, m.content, m.created_at),
        )
        self.conn.commit()
        return m

    def get_messages(self, conversation_id: str) -> List[ChatMessage]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,)
        ).fetchall()
        return [ChatMessage(**dict(r)) for r in rows]
