"""
Main entry point for the memory-augmented chat app.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from database.schema import init_db
from memory.store import MemoryStore
from strategies.baseline import BaselineStrategy
from ui.chat import render_chat
from ui.memory_sidebar import render_sidebar

st.set_page_config(
    page_title="Memory Chat",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------- styling

st.markdown("""
<style>
    .block-container { padding-top: 2rem; max-width: 900px; }
    section[data-testid="stSidebar"] { width: 340px !important; }
    div[data-testid="stChatMessage"] { padding: 0.5rem 0; }
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------ init

init_db()


@st.cache_resource
def get_store() -> MemoryStore:
    return MemoryStore()


@st.cache_resource
def get_strategy(_store: MemoryStore) -> BaselineStrategy:
    return BaselineStrategy(_store)


store = get_store()
strategy = get_strategy(store)

if "conversation_id" not in st.session_state:
    conv = store.create_conversation()
    st.session_state["conversation_id"] = conv.id

if not os.environ.get("GROQ_API_KEY"):
    st.warning(
        "⚠️ GROQ_API_KEY is not set. Copy `.env.example` to `.env` and add your key, "
        "or set it in your environment before chatting.",
        icon="⚠️",
    )

# ------------------------------------------------------------------ UI

st.title("🧠 Memory Chat")
st.caption("An LLM chat app with transparent, editable long-term memory · powered by Groq")

render_sidebar(store, strategy)
render_chat(store, strategy)

if st.session_state.get("memory_error"):
    st.toast(f"Memory processing hiccup: {st.session_state['memory_error']}", icon="⚠️")
    st.session_state["memory_error"] = None
