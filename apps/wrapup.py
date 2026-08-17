"""Season wrap-up dashboard (stub). Run: streamlit run apps/wrapup.py"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from cheddarbowl import config  # noqa: E402

st.set_page_config(page_title="Cheddarbowl Wrap-Up", page_icon="🧀", layout="wide")
st.title("🧀 Cheddarbowl — Season Wrap-Up")
st.info(f"End-of-season wrap-up for {config.SEASON} is coming soon.")
