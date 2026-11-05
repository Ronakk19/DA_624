"""
Baseline memory strategy.

Pipeline per turn:
  1. Ask the LLM to extract candidate memories from the (user, assistant) turn.
  2. For each candidate, find the most similar existing active memories.
  3. Decide what to do with it. Three policies are available (POLICY below),
     so they can be compared against each other in evaluation/locomo_eval.py:

     "decision"  (default) -- the LLM sees the candidate together with the
                 top-k similar memories in ONE call and picks
                 add / update(target) / skip / conflict(target).
                 Similar to Mem0's ADD / UPDATE / NOOP decision step.
     "relation"  -- compare only against the single closest memory; the LLM
                 labels the pair duplicate / refines / complementary /
                 contradicts and complementary facts are kept separately.
     "overwrite" -- the original behaviour: compare against the closest
                 memory and, unless it is a confident conflict, replace it.

  4. A confident conflict never overwrites anything: it becomes a pending
     CONFLICT event that the user resolves in the sidebar.

"Confident" is governed by CONFLICT_CONFIDENCE_THRESHOLD -- tune this
to make the system more or less trigger-happy about asking the user.
"""

from __future__ import annotations

from typing import List, Optional

from memory.models import Memory, MemoryEvent, MemoryType, MemoryScope, MemoryStatus, EventType
from memory.store import MemoryStore
from llm.client import LLMClient
from llm.prompts import build_extraction_messages, build_conflict_messages, build_decision_messages
from strategies.base import BaseMemoryStrategy

CONFLICT_CONFIDENCE_THRESHOLD = 0.6
# "relation"/"overwrite": similarity above this against the closest memory = "same topic"
SIMILARITY_MATCH_THRESHOLD = 0.35
# "decision": how many similar memories the LLM gets to see, and the minimum
# similarity for a memory to be shown at all (kept low on purpose -- the LLM,
# not the similarity score, decides whether two memories are about the same thing)
DECISION_TOP_K = 5
DECISION_MIN_SIMILARITY = 0.05

POLICY = "decision"
POLICIES = ("decision", "relation", "overwrite")


class BaselineStrategy(BaseMemoryStrategy):
    def __init__(self, store: MemoryStore, llm: Optional[LLMClient] = None,
                 non_conflict_policy: str = POLICY):
        if non_conflict_policy not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}")
        self.store = store
        self.llm = llm  # lazily created if None, so import-time doesn't require an API key
        self.policy = non_conflict_policy

    def _get_llm(self) -> LLMClient:
        if self.llm is None:
            from llm.client import get_client
            self.llm = get_client()
        return self.llm

    # ------------------------------------------------------------ pipeline

    def process_turn(self, conversation_id: str, user_message: str, assistant_message: str) -> List[MemoryEvent]:
        llm = self._get_llm()
        source = f"Conversation {conversation_id[-6:]}"
        try:
            extraction = llm.json_completion(build_extraction_messages(user_message, assistant_message))
        except Exception:
            return []
        candidates = extraction.get("memories", []) if isinstance(extraction, dict) else []
        return self.apply_candidates(conversation_id, candidates, source)

    def apply_candidates(self, conversation_id: str, candidates: List[dict], source: str,
                         auto_resolve: Optional[str] = None) -> List[MemoryEvent]:
        """
        Save / update / skip / flag-conflict for already-extracted candidates.
        Shared by the chat app and the LoCoMo evaluation.

        auto_resolve: None (default) leaves conflicts pending for the user, as in
        the app; "new" accepts the new content immediately (no-human eval mode).
        """
        events: List[MemoryEvent] = []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            content = (cand.get("content") or "").strip()
            if not content:
                continue
            new_mem = Memory(
                content=content,
                type=self._safe_enum(MemoryType, cand.get("type"), MemoryType.OTHER),
                scope=self._safe_enum(MemoryScope, cand.get("scope"), MemoryScope.LONG_TERM),
                importance=self._safe_float(cand.get("importance"), 0.5),
                confidence=self._safe_float(cand.get("confidence"), 0.7),
                provenance=source, conversation_id=conversation_id,
            )
            if self.policy == "decision":
                self._apply_decision(new_mem, source, events, auto_resolve)
            else:
                self._apply_pairwise(new_mem, source, events, auto_resolve)
        return events

    # --- policy: decision (top-k, one LLM call) --------------------------------

    def _apply_decision(self, new_mem: Memory, source: str, events: list, auto_resolve) -> None:
        similar = [(m, s) for m, s in self.store.search(new_mem.content, top_k=DECISION_TOP_K)
                   if s >= DECISION_MIN_SIMILARITY]
        if not similar:
            self._save(new_mem, source, events)
            return
        if any(m.content.strip().lower() == new_mem.content.lower() for m, _ in similar):
            return  # exact duplicate

        try:
            verdict = self._get_llm().json_completion(
                build_decision_messages(new_mem.content, [m.content for m, _ in similar]))
        except Exception:
            verdict = {"action": "add"}
        if not isinstance(verdict, dict):
            verdict = {"action": "add"}

        action = str(verdict.get("action", "add")).lower()
        reason = verdict.get("reason", "") or ""
        conf = self._safe_float(verdict.get("confidence"), 0.0)
        target = self._target(verdict.get("target"), similar)

        if action == "skip" and target is not None:
            return
        if action == "update" and target is not None:
            merged = verdict.get("merged")
            content = merged.strip() if isinstance(merged, str) and merged.strip() else new_mem.content
            self._update(target, content, reason or "New conversation refines this memory.",
                         source, events)
            return
        if action == "conflict" and target is not None and conf >= CONFLICT_CONFIDENCE_THRESHOLD:
            self._conflict(target, new_mem.content, reason, verdict.get("options"), source,
                           events, auto_resolve)
            return
        # "add", an unconfident conflict, or an unusable answer: keep both
        self._save(new_mem, source, events, reason or None)

    @staticmethod
    def _target(value, similar):
        try:
            idx = int(value) - 1
        except (TypeError, ValueError):
            return None
        return similar[idx][0] if 0 <= idx < len(similar) else None

    # --- policies: relation / overwrite (closest memory only) ------------------

    def _apply_pairwise(self, new_mem: Memory, source: str, events: list, auto_resolve) -> None:
        match = self._find_closest_active(new_mem.content)
        if match is None:
            self._save(new_mem, source, events)
            return
        existing, _ = match
        if existing.content.strip().lower() == new_mem.content.lower():
            return

        try:
            verdict = self._get_llm().json_completion(
                build_conflict_messages(existing.content, new_mem.content))
        except Exception:
            verdict = {}
        if not isinstance(verdict, dict):
            verdict = {}

        is_conflict = bool(verdict.get("conflict"))
        conf = self._safe_float(verdict.get("confidence"), 0.0)
        relation = str(verdict.get("relation", "")).lower()
        reason = verdict.get("reason", "") or ""

        if is_conflict and conf >= CONFLICT_CONFIDENCE_THRESHOLD:
            self._conflict(existing, new_mem.content, reason, verdict.get("options"), source,
                           events, auto_resolve)
        elif self.policy == "relation" and relation == "duplicate":
            return
        elif self.policy == "relation" and relation in ("complementary", "contradicts"):
            self._save(new_mem, source, events, reason or "Related but distinct from an existing memory")
        else:
            self._update(existing, new_mem.content, reason or "New conversation refines this memory.",
                         source, events)

    def _find_closest_active(self, content: str):
        results = self.store.search(content, top_k=1, status=MemoryStatus.ACTIVE)
        if results and results[0][1] >= SIMILARITY_MATCH_THRESHOLD:
            return results[0]
        return None

    # --- shared actions ----------------------------------------------------------

    def _save(self, mem: Memory, source: str, events: list, reason: Optional[str] = None) -> None:
        self.store.create(mem, reason=reason or "New information extracted from conversation", source=source)
        events.append(MemoryEvent(type=EventType.SAVED, memory_id=mem.id, after=mem.content, source=source))

    def _update(self, existing: Memory, content: str, reason: str, source: str, events: list) -> None:
        before = existing.content
        updated = self.store.update(existing.id, content, reason=reason, source=source)
        events.append(MemoryEvent(type=EventType.UPDATED, memory_id=updated.id,
                                  before=before, after=content, source=source))

    def _conflict(self, existing: Memory, content: str, reason: str, options, source: str,
                  events: list, auto_resolve) -> None:
        evt = MemoryEvent(
            type=EventType.CONFLICT, memory_id=existing.id,
            before=existing.content, after=content,
            reason=reason or "New evidence may contradict an existing memory.",
            source=source, resolved=False,
            options=[o for o in (options or []) if isinstance(o, str) and o.strip()],
        )
        self.store.events.add(evt)
        events.append(evt)
        if auto_resolve == "new":
            self.resolve_conflict(evt.id, content)

    # ------------------------------------------------------------ retrieval

    def retrieve_for_query(self, query: str, top_k: int = 5) -> List[tuple]:
        return self.store.search(query, top_k=top_k)

    # ------------------------------------------------------------ conflicts

    def resolve_conflict(self, event_id: str, chosen_content: str) -> Memory:
        """Apply the user's (or auto-accept's) chosen resolution for a pending conflict."""
        evt = self.store.events.get(event_id)
        if evt is None or evt.memory_id is None:
            raise ValueError("Conflict event not found or has no associated memory")
        updated = self.store.update(evt.memory_id, chosen_content,
                                    reason="Conflict resolved", source=evt.source)
        self.store.events.resolve(event_id)
        return updated

    def dismiss_conflict(self, event_id: str) -> None:
        """User chose 'Keep existing' / 'Don't store this' -- leave existing memory as-is."""
        self.store.events.resolve(event_id)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _safe_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_enum(enum_cls, value, default):
        try:
            return enum_cls(value)
        except Exception:
            return default
