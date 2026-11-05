"""
Sidebar: live memory event stream + quick stats + entry points into the
memory explorer and "add memory" flow. This is what makes the memory
system feel alive rather than a hidden background process.
"""

from __future__ import annotations

import streamlit as st

from memory.store import MemoryStore
from memory.models import Memory, MemoryType, MemoryScope, EventType
from strategies.baseline import BaselineStrategy
from ui.memory_details import render_event_expander
from ui.conflict_dialog import render_conflict


def render_sidebar(store: MemoryStore, strategy: BaselineStrategy) -> None:
    with st.sidebar:
        st.markdown("## 🧠 Memory")

        pending = store.events.pending_conflicts()
        if pending:
            st.warning(f"{len(pending)} conflict{'s' if len(pending) != 1 else ''} need your input")
            for evt in pending:
                render_conflict(store, strategy, evt)
            st.divider()

        st.markdown("#### Recent activity")
        recent = store.events.recent(limit=12)
        if not recent:
            st.caption("No memory activity yet -- start chatting.")
        else:
            for evt in recent:
                if evt.type == EventType.CONFLICT and not evt.resolved:
                    continue  # already shown above
                render_event_expander(evt)

        st.divider()

        counts = store.counts()
        active = counts.get("active", 0)
        compressed = counts.get("compressed", 0)
        deleted = counts.get("deleted", 0)
        c1, c2, c3 = st.columns(3)
        c1.metric("Active", active)
        c2.metric("Compressed", compressed)
        c3.metric("Deleted", deleted)

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("📂 View all memories", use_container_width=True):
                st.session_state["show_explorer"] = True
        with col_b:
            if st.button("＋ Add memory", use_container_width=True):
                st.session_state["show_add_memory"] = True

        if st.session_state.get("show_add_memory"):
            _render_add_memory_form(store)


def _render_add_memory_form(store: MemoryStore) -> None:
    with st.form("add_memory_form", clear_on_submit=True):
        st.markdown("**Add a memory manually**")
        content = st.text_area("Content", placeholder="User prefers concise answers.")
        col1, col2 = st.columns(2)
        with col1:
            mem_type = st.selectbox("Type", [t.value for t in MemoryType])
        with col2:
            mem_scope = st.selectbox("Scope", [s.value for s in MemoryScope])
        importance = st.slider("Importance", 0.0, 1.0, 0.5)
        submitted = st.form_submit_button("Save memory")
        if submitted and content.strip():
            store.create(
                Memory(content=content.strip(), type=MemoryType(mem_type),
                       scope=MemoryScope(mem_scope), importance=importance,
                       confidence=1.0, provenance="Manually added"),
                reason="Manually added by user", source="user",
            )
            st.session_state["show_add_memory"] = False
            st.rerun()
