"""
Rollup metrics over the memory store's event log, used by the Evaluation
page and available for scripted analysis via evaluation/runner.py.
"""

from __future__ import annotations

from memory.store import MemoryStore
from memory.models import EventType


def compute_summary_metrics(store: MemoryStore) -> dict:
    mem_counts = store.counts()
    event_counts = store.events.counts()
    pending_conflicts = len(store.events.pending_conflicts())

    return {
        "total_memories": sum(mem_counts.values()),
        "active": mem_counts.get("active", 0),
        "compressed": mem_counts.get("compressed", 0),
        "deleted": mem_counts.get("deleted", 0),
        "conflicts_raised": event_counts.get(EventType.CONFLICT.value, 0),
        "conflicts_pending": pending_conflicts,
        "updates": event_counts.get(EventType.UPDATED.value, 0),
        "compressions": event_counts.get(EventType.COMPRESSED.value, 0),
        "edits": event_counts.get(EventType.EDITED.value, 0),
        "event_counts": event_counts,
    }
