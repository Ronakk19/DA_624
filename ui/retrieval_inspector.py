"""
Debug/developer panel: shows the last query, the memories retrieved for it
with scores, the final assembled prompt, and an approximate token count.
Purpose: make it possible to tell whether a bad response came from
extraction, management, retrieval, or the LLM's reasoning itself.
"""

from __future__ import annotations

import streamlit as st

from memory.store import MemoryStore


def render_retrieval_inspector(store: MemoryStore) -> None:
    st.markdown("## 🔍 Retrieval Inspector")
    conversation_id = st.session_state.get("conversation_id")
    if not conversation_id:
        st.info("Start a conversation to see retrieval data.")
        return

    data = store.last_retrieval(conversation_id)
    if not data:
        st.info("No retrieval logged yet for this conversation. Send a chat message first.")
        return

    st.markdown("**Current query:**")
    st.code(data["query"], language=None)

    st.markdown("**Retrieved memories:**")
    if not data["retrieved"]:
        st.caption("(none retrieved above threshold)")
    else:
        for i, item in enumerate(data["retrieved"], start=1):
            st.markdown(f"**{i}. Memory `{item['memory_id']}`** — score: `{item['score']:.2f}`")
            st.caption(item["content"])

    with st.expander("Final assembled prompt"):
        st.code(data.get("final_prompt", ""), language=None)

    st.metric("Approx. token count", data.get("token_estimate", 0))

    st.divider()
    st.markdown("#### Recent conversation")
    messages = store.get_messages(conversation_id)
    for m in messages[-10:]:
        st.markdown(f"**{m.role}:** {m.content}")
