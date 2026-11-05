"""
Main chat panel: message history + input box, using native
st.chat_message / st.chat_input so it looks like a normal LLM chat app.
"""

from __future__ import annotations

import os

import streamlit as st

from llm.client import get_client
from llm.prompts import build_chat_messages
from memory.store import MemoryStore
from strategies.baseline import BaselineStrategy


# How many recent chat messages (user + assistant) the LLM sees as short-term
# context. Everything older reaches the model only through long-term memory.
HISTORY_MESSAGES = int(os.environ.get("HISTORY_MESSAGES", "6"))

CUTOFF_NOTE = ("🔒 You changed memory here. Messages above are hidden from the assistant — "
               "it now relies on the memory list, so edited or deleted facts don't leak back in.")


def context_messages(store: MemoryStore, conversation_id: str) -> list:
    """
    The chat messages actually sent to the LLM: only those after the user's last
    memory edit/delete/conflict resolution, and at most HISTORY_MESSAGES of them.
    Without this, deleting "User studies at IIT Guwahati" from memory changes
    nothing, because the original chat message is still in the prompt.
    """
    cutoff = store.last_user_memory_change()
    msgs = store.get_messages(conversation_id)
    if cutoff:
        msgs = [m for m in msgs if m.created_at >= cutoff]
    return msgs[-HISTORY_MESSAGES:] if HISTORY_MESSAGES > 0 else msgs


def render_chat(store: MemoryStore, strategy: BaselineStrategy) -> None:
    conversation_id = st.session_state["conversation_id"]

    messages = store.get_messages(conversation_id)
    cutoff = store.last_user_memory_change()
    divider_shown = False

    for m in messages:
        if cutoff and not divider_shown and m.created_at >= cutoff:
            st.caption(CUTOFF_NOTE)
            divider_shown = True
        with st.chat_message(m.role):
            st.markdown(m.content)
    if cutoff and messages and not divider_shown and messages[0].created_at < cutoff:
        st.caption(CUTOFF_NOTE)

    user_input = st.chat_input("Message your assistant...")
    if not user_input:
        return

    store.add_message(conversation_id, "user", user_input)
    with st.chat_message("user"):
        st.markdown(user_input)

    # --- retrieval ---
    retrieved = strategy.retrieve_for_query(user_input, top_k=5)

    history = [{"role": m.role, "content": m.content} for m in context_messages(store, conversation_id)]
    prompt_messages = build_chat_messages(history, retrieved)

    approx_tokens = sum(len(m["content"]) for m in prompt_messages) // 4

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_text = ""
        try:
            client = get_client()
            stream = client.chat(prompt_messages, stream=True)
            for chunk in stream:
                full_text += chunk
                placeholder.markdown(full_text + "▌")
            placeholder.markdown(full_text)
        except Exception as e:
            full_text = f"⚠️ Error calling the LLM: {e}"
            placeholder.markdown(full_text)

    store.add_message(conversation_id, "assistant", full_text)

    store.log_retrieval(
        conversation_id=conversation_id,
        query=user_input,
        retrieved=retrieved,
        final_prompt="\n".join(f"[{m['role']}] {m['content']}" for m in prompt_messages),
        token_estimate=approx_tokens,
    )

    # --- memory extraction (best-effort, never break the chat if it fails) ---
    try:
        new_events = strategy.process_turn(conversation_id, user_input, full_text)
        if new_events:
            st.session_state["last_memory_events"] = new_events
    except Exception as e:
        st.session_state["memory_error"] = str(e)

    st.rerun()
