@echo off
REM Launch the Cheddarbowl draft assistant using the project's virtualenv (Streamlit 1.60),
REM so it never accidentally runs under a different global Python. Double-click or run: run.bat
cd /d "%~dp0"
".venv\Scripts\python.exe" -m streamlit run apps/draft.py
