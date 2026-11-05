"""
Rendering for a single memory event as an expandable card, and for full
memory detail (used by the explorer page).
"""

from __future__ import annotations

import streamlit as st

from memory.models import MemoryEvent, Memory, EventType

_LABEL = {
    EventType.SAVED: "Saved",
    EventType.UPDATED: "Updated",
    EventType.CONFLICT: "Conflict",
    EventType.COMPRESSED: "Compressed",
    EventType.EDITED: "Edited",
    EventType.DELETED: "Deleted",
    EventType.RESTORED: "Restored",
}


def render_event_expander(evt: MemoryEvent) -> None:
    label = _LABEL.get(evt.type, evt.type.value.title())
    headline = evt.after or evt.before or "(no content)"
    headline = (headline[:60] + "…") if len(headline) > 60 else headline

    with st.expander(f"{evt.icon()} {label} · {headline}", expanded=False):
        if evt.before and evt.after:
            st.markdown("**Before:**")
            st.caption(evt.before)
            st.markdown("**After:**")
            st.caption(evt.after)
        elif evt.after:
            st.caption(evt.after)
        elif evt.before:
            st.caption(evt.before)

        if evt.reason:
            st.markdown("**Reason:**")
            st.caption(evt.reason)
        if evt.source:
            st.markdown("**Source:**")
            st.caption(evt.source)
        st.caption(f"_{evt.created_at}_")


def render_memory_card(memory: Memory, store) -> None:
    """Full detail card for the Memory Explorer, with actions."""
    st.markdown(f"### {memory.content}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Type", memory.type.value)
    c2.metric("Scope", memory.scope.value)
    c3.metric("Importance", f"{memory.importance:.2f}")
    c4.metric("Confidence", f"{memory.confidence:.2f}")

    st.caption(f"Status: **{memory.status.value}** · Version {memory.version}")
    st.caption(f"Created: {memory.created_at}  ·  Updated: {memory.updated_at}")
    if memory.provenance:
        st.caption(f"Provenance: {memory.provenance}")

    with st.expander("Version history"):
        history = store.version_history(memory.id)
        for v in history:
            st.markdown(f"**v{v.version}** — {v.created_at}")
            st.caption(v.content)
            if v.reason:
                st.caption(f"_Reason: {v.reason}_")
            st.divider()

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("✎ Edit", key=f"edit_{memory.id}"):
            st.session_state[f"editing_{memory.id}"] = True
    with col2:
        if memory.status.value != "deleted":
            if st.button("🗑 Delete", key=f"del_{memory.id}"):
                store.delete(memory.id)
                st.rerun()
        else:
            if st.button("⟲ Restore", key=f"restore_{memory.id}"):
                store.restore(memory.id)
                st.rerun()
    with col3:
        st.caption(f"id: `{memory.id}`")

    if st.session_state.get(f"editing_{memory.id}"):
        new_content = st.text_area("Edit content", value=memory.content, key=f"edit_area_{memory.id}")
        if st.button("Save changes", key=f"save_{memory.id}"):
            store.edit(memory.id, new_content)
            st.session_state[f"editing_{memory.id}"] = False
            st.rerun()
