"""
Abstract interface for a memory management strategy.

A "strategy" owns the policy of *when* to extract, save, update, flag a
conflict, or compress memories. Separating this from MemoryStore (which
just persists things) means you can swap in a smarter/cheaper strategy
later without touching storage or UI code, and makes it possible to
evaluate different strategies against the same store in evaluation/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from memory.models import Memory, MemoryEvent


class BaseMemoryStrategy(ABC):
    @abstractmethod
    def process_turn(self, conversation_id: str, user_message: str, assistant_message: str) -> List[MemoryEvent]:
        """
        Given a completed user/assistant turn, extract candidate memories,
        check for conflicts against existing memories, and save/update/flag
        as appropriate. Returns the list of events produced.
        """
        ...

    @abstractmethod
    def retrieve_for_query(self, query: str, top_k: int = 5) -> List[tuple]:
        """Return [(Memory, score), ...] relevant to the given query."""
        ...
