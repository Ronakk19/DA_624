"""
Evaluation / debug page: the retrieval inspector plus rollup metrics on
memory system activity, useful for diagnosing whether issues stem from
extraction, management, retrieval, or the LLM's reasoning.
"""

from __future__ import annotations

import streamlit as st

from database.schema import init_db
from memory.store import MemoryStore
from ui.retrieval_inspector import render_retrieval_inspector
from evaluation.metrics import compute_summary_metrics

st.set_page_config(page_title="Evaluation", page_icon="📊", layout="wide")
init_db()


@st.cache_resource
def get_store() -> MemoryStore:
    return MemoryStore()


store = get_store()

st.title("📊 Evaluation & Debugging")

tab1, tab2 = st.tabs(["Retrieval Inspector", "System Metrics"])

with tab1:
    render_retrieval_inspector(store)

with tab2:
    metrics = compute_summary_metrics(store)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total memories", metrics["total_memories"])
    c2.metric("Active", metrics["active"])
    c3.metric("Conflicts raised", metrics["conflicts_raised"])
    c4.metric("Conflicts pending", metrics["conflicts_pending"])

    c5, c6, c7 = st.columns(3)
    c5.metric("Updates", metrics["updates"])
    c6.metric("Compressions", metrics["compressions"])
    c7.metric("Manual edits", metrics["edits"])

    st.markdown("#### Event type breakdown")
    st.bar_chart(metrics["event_counts"])
