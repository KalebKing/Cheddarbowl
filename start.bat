@echo off
REM ==========================================================================
REM  Cheddarbowl Draft Assistant -- one-click launcher (single-window flow).
REM  Runs the orchestrator: opens ONE browser (your league page + a chromeless
REM  dashboard window) and runs the scraper + app hidden in the background.
REM  Double-click this. Log into ESPN once, open your draft, and go.
REM
REM  (Supersedes run-draft.bat's four-window flow. Step 4 will package this as a
REM   single .exe with no console window at all.)
REM ==========================================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" launch.py
