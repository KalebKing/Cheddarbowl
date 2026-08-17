"""Weekly dashboard (stub) — pre/post-game views. Run: streamlit run apps/weekly.py"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from cheddarbowl import config  # noqa: E402

st.set_page_config(page_title="Cheddarbowl Weekly", page_icon="🧀", layout="wide")
st.title("🧀 Cheddarbowl — Weekly")
st.info(f"Weekly pre/post-game analysis for season {config.SEASON} is coming soon.")
