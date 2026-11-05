"""
Memory Explorer: filterable browser over every memory in the system, with
full detail (provenance, version history) and management actions.
"""

from __future__ import annotations

import streamlit as st

from database.schema import init_db
from memory.store import MemoryStore
from memory.models import MemoryStatus, MemoryScope
from ui.memory_details import render_memory_card

st.set_page_config(page_title="Memory Explorer", page_icon="📂", layout="wide")
init_db()


@st.cache_resource
def get_store() -> MemoryStore:
    return MemoryStore()


store = get_store()

st.title("📂 Memory Explorer")

col1, col2, col3 = st.columns(3)
with col1:
    status_filter = st.selectbox(
        "Status",
        ["All", "Active", "Compressed", "Deleted", "Temporary"],
    )
with col2:
    scope_filter = st.selectbox(
        "Scope",
        ["All", "Session", "Long term", "Task"],
    )
with col3:
    show_conflicts_only = st.checkbox("Conflicts only (pending)")

search_query = st.text_input("Search memory content", placeholder="e.g. python, NLP, coffee...")

status = None if status_filter == "All" else MemoryStatus(status_filter.lower())
scope = None if scope_filter == "All" else MemoryScope(scope_filter.lower().replace(" ", "_"))

if show_conflicts_only:
    pending = store.events.pending_conflicts()
    st.markdown(f"### {len(pending)} pending conflict(s)")
    for evt in pending:
        mem = store.get(evt.memory_id) if evt.memory_id else None
        with st.container(border=True):
            st.markdown(f"**Existing:** {evt.before}")
            st.markdown(f"**New evidence:** {evt.after}")
            st.caption(evt.reason)
    st.stop()

memories = store.list(status=status, scope=scope.value if scope else None)

if search_query:
    q = search_query.lower()
    memories = [m for m in memories if q in m.content.lower()]

st.caption(f"{len(memories)} memories")

for memory in memories:
    with st.container(border=True):
        render_memory_card(memory, store)
