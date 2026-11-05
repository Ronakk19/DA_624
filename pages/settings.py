"""
Settings page: model selection and strategy tuning knobs. Values are held
in session_state for the current session; wire these into llm/client.py
and strategies/baseline.py defaults if you want them to persist further.
"""

from __future__ import annotations

import os
import streamlit as st

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="centered")

st.title("⚙️ Settings")

st.markdown("#### Groq API")
key_set = bool(os.environ.get("GROQ_API_KEY"))
st.write("API key detected ✅" if key_set else "API key **not** detected ❌ — set `GROQ_API_KEY` in `.env`")

st.markdown("#### Model")
model = st.text_input(
    "Chat model",
    value=os.environ.get("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile"),
    help="Any Groq-hosted chat model id.",
)
if st.button("Apply for this session"):
    os.environ["GROQ_CHAT_MODEL"] = model
    st.success(f"Using {model} for this session.")

st.divider()

st.markdown("#### Memory strategy")
st.caption("These mirror the thresholds in `strategies/baseline.py`. Editing them here only "
           "affects display -- change the constants in that file to actually retune behavior.")
from memory.store import retrieval_backend
from strategies import baseline as _b
from ui.chat import HISTORY_MESSAGES
st.write(f"**Memory policy:** `{_b.POLICY}`  ·  **Retrieval:** {retrieval_backend()}")
st.write(f"**Short-term chat window:** last {HISTORY_MESSAGES} messages "
         "(set `HISTORY_MESSAGES` in `.env`)")
st.slider("Conflict confidence threshold", 0.0, 1.0, _b.CONFLICT_CONFIDENCE_THRESHOLD, disabled=True)
st.caption("For semantic retrieval run `pip install sentence-transformers` and restart the app.")

st.divider()
st.caption("Restart the app after changing environment variables in `.env` for them to fully take effect.")
