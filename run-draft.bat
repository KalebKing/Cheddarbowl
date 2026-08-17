@echo off
REM ==========================================================================
REM  Cheddarbowl draft assistant -- LIVE DRAFT one-click launcher.
REM  Starts everything needed to track a live ESPN draft, in order:
REM    1) a debug-enabled Edge (its OWN login profile) pointed at ESPN fantasy
REM    2) the live websocket scraper (espn_live) -- waits for the draft worker
REM    3) the Streamlit dashboard
REM
REM  Draft day: double-click this, log into ESPN in the Edge window that opens
REM  (one time -- the login is saved), open your draft room, and switch the app
REM  to Live mode. Picks flow in automatically.
REM
REM  NOTE: to track your REAL league, make sure runtime\active_league.json is absent
REM  (that file overrides config to a mock). Delete it before a real draft.
REM ==========================================================================
setlocal
cd /d "%~dp0"

REM --- scraper is externally managed in this launcher (avoid dashboard respawn race) ---
set "CB_SCRAPER_MANAGED=1"

REM --- this launcher starts a BRAND-NEW draft (clean slate below), so tell the scraper NOT to
REM     recover prior picks. A mid-draft SCRAPER restart (app-spawned / manual) recovers by default;
REM     only this explicit "new draft" entry point starts empty. Robust even if a delete below fails. ---
set "CB_FRESH_DRAFT=1"

REM --- never write .pyc in the dev workflow: editing source between runs can otherwise leave a
REM     stale __pycache__ that a "fresh" process still loads, causing confusing AttributeError
REM     crashes on attributes the current source clearly has. Always compile from source instead. ---
set "PYTHONDONTWRITEBYTECODE=1"

REM --- locate Edge ----------------------------------------------------------
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
  echo [!] Could not find msedge.exe -- edit EDGE in this script to your Edge path.
  pause
  exit /b 1
)

REM --- kill any orphaned app process from a previous run FIRST, so a zombie streamlit can't keep
REM     holding port 8501 and serving stale code (closing the window doesn't always reap children) ---
echo Clearing any leftover app processes ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'Cheddarbowl' -and ($_.CommandLine -match 'streamlit.*apps.*draft\.py' -or $_.CommandLine -match 'cheddarbowl\.ingest\.espn_live') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

REM --- fresh state for a new draft (stale picks / lock from a prior run) -----
del /q "%~dp0runtime\draft_state.json"    2>nul
del /q "%~dp0runtime\ws_capture.jsonl"    2>nul
REM  Do NOT delete scraper.lock: the filelock is the single-instance guard, and a leftover file is
REM  harmless (filelock re-acquires the OS lock on it). Deleting it while a prior scraper is still
REM  alive lets a duplicate acquire a fresh lock and run concurrently -- the spawn-storm that
REM  worsened the live-draft outage. The scraper releases its own lock when it exits.
del /q "%~dp0runtime\pick_overrides.json" 2>nul
REM --- no host token in dev: absent token file => this single-user run gets full host controls ---
del /q "%~dp0secrets\host_token.txt"       2>nul

REM --- 1) debug Edge with a persistent login profile ------------------------
echo Launching Edge (debug profile) ...
start "" "%EDGE%" --remote-debugging-port=9222 --user-data-dir="%~dp0runtime\edge_cdp" --no-first-run --no-default-browser-check "https://fantasy.espn.com/football/league?leagueId=754228479"

REM --- 2) live websocket scraper (own window so you can watch picks) ---------
echo Starting live scraper ...
start "ESPN live scraper" ".venv\Scripts\python.exe" -u -m cheddarbowl.ingest.espn_live

REM --- 3) dashboard (foreground) --------------------------------------------
REM  --server.fileWatcherType none: draft day, don't hot-reload on an accidental edit (a mid-draft
REM  reload can desync modules and crash the app).
echo Starting dashboard ...
".venv\Scripts\python.exe" -m streamlit run apps/draft.py --server.fileWatcherType none

endlocal
