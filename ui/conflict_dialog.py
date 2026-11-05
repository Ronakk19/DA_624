"""
Conflict resolution UI. Renders LLM-generated candidate resolutions as
buttons, plus a "keep existing" / "don't store" / custom option, per the
conflict UI spec.
"""

from __future__ import annotations

import streamlit as st

from memory.store import MemoryStore
from memory.models import MemoryEvent
from strategies.baseline import BaselineStrategy


def render_conflict(store: MemoryStore, strategy: BaselineStrategy, evt: MemoryEvent) -> None:
    existing = store.get(evt.memory_id) if evt.memory_id else None
    existing_content = existing.content if existing else evt.before

    with st.container(border=True):
        st.markdown("**⚠ MEMORY CONFLICT**")
        st.markdown("**Existing:**")
        st.caption(existing_content)
        st.markdown("**New evidence:**")
        st.caption(evt.after)
        if evt.reason:
            st.caption(f"_{evt.reason}_")

        st.markdown("**What should I remember?**")

        options = evt.options or []
        for i, opt in enumerate(options):
            if st.button(opt, key=f"conflict_{evt.id}_opt_{i}", use_container_width=True):
                strategy.resolve_conflict(evt.id, opt)
                st.rerun()

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Keep existing", key=f"conflict_{evt.id}_keep", use_container_width=True):
                strategy.dismiss_conflict(evt.id)
                st.rerun()
        with col2:
            if st.button("Don't store this", key=f"conflict_{evt.id}_skip", use_container_width=True):
                strategy.dismiss_conflict(evt.id)
                st.rerun()

        with st.expander("+ Custom response"):
            custom = st.text_input("Custom memory content", key=f"conflict_{evt.id}_custom")
            if st.button("Save custom", key=f"conflict_{evt.id}_custom_save"):
                if custom.strip():
                    strategy.resolve_conflict(evt.id, custom.strip())
                    st.rerun()
