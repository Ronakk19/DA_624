"""
Abstract interface for a memory store.

Defining this separately from the concrete SQLite implementation means the
backend could later be swapped (e.g. for Postgres or a vector DB) without
touching UI or strategy code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from memory.models import Memory, MemoryEvent, MemoryStatus


class BaseMemoryStore(ABC):
    @abstractmethod
    def create(self, memory: Memory, reason: str = "", source: str = "") -> Memory: ...

    @abstractmethod
    def update(self, memory_id: str, new_content: str, reason: str = "", source: str = "") -> Memory: ...

    @abstractmethod
    def get(self, memory_id: str) -> Optional[Memory]: ...

    @abstractmethod
    def list(self, status: Optional[MemoryStatus] = None, scope: Optional[str] = None) -> List[Memory]: ...

    @abstractmethod
    def delete(self, memory_id: str, reason: str = "") -> None: ...

    @abstractmethod
    def restore(self, memory_id: str) -> None: ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[tuple]: ...
