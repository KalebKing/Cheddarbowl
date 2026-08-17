"""Auto-start and health of the live scraper, for the dashboard.

The app calls ``ensure_running()`` on every Live-mode render; it spawns the poller only when
one isn't already alive. True single-instance is guaranteed by the poller itself (it holds an
exclusive ``filelock`` for its lifetime -- see nfl_scraper._cmd_poll), so even a redundant
spawn just exits immediately. ``health()`` reads the poller's heartbeat for the UI + failsafe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from cheddarbowl import config

# Module-global (persists across Streamlit reruns in the one server process): throttles spawn
# attempts so we don't relaunch every 3s rerun before a fresh poller has written its heartbeat.
_last_spawn: float = 0.0


def read_status() -> dict | None:
    """Parse the poller heartbeat file; None if missing/unreadable."""
    try:
        with open(config.SCRAPER_STATUS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def health() -> dict:
    """UI-facing health: state in {running, error, stale, down}, plus picks/age/error."""
    st = read_status()
    if not st:
        return {"state": "down", "picks": 0, "age": None, "error": None}
    age = time.time() - float(st.get("ts", 0))
    if age > config.SCRAPER_STALE_SECONDS:
        state = "stale"                       # heartbeat went quiet -> process likely dead
    elif not st.get("ok", True):
        state = "error"                       # alive but failing to scrape (auth? layout?)
    elif st.get("stale"):
        state = "preseason"                   # running fine, but rosters are last-season carryover
    else:
        state = "running"
    return {"state": state, "picks": int(st.get("picks", 0)), "age": age,
            "error": st.get("error"), "source": st.get("source", "")}


def is_running() -> bool:
    return read_status() is not None and health()["state"] in ("running", "error", "preseason")


def _lock_held() -> bool:
    """True if another process currently holds the scraper's single-instance lock. This is the
    AUTHORITATIVE 'a scraper is alive' signal -- independent of the heartbeat, which can go briefly
    stale during a heavy REST reconcile. Best-effort, and crucially non-stealing: if the lock is
    free we acquire+release it instantly (a leftover lock file from a prior run is thus harmless);
    if it's held we get Timeout and report True without disturbing the holder."""
    try:
        from filelock import FileLock, Timeout
    except Exception:  # noqa: BLE001 -- filelock missing -> fall back to heartbeat-only logic
        return False
    lk = FileLock(config.SCRAPER_LOCK)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True                           # someone else holds it -> a scraper is alive
    except Exception:  # noqa: BLE001
        return False
    else:
        lk.release()
        return False


def ensure_running() -> None:
    """Spawn the live scraper unless one is already alive (or we just spawned one).

    Idempotent by design: the scraper is a hard singleton via its filelock, so even a redundant
    spawn exits immediately without touching state. We nonetheless avoid the redundant spawn here --
    checking the lock first (authoritative) then the heartbeat + a local throttle -- so we never
    pile up processes even while a heartbeat is momentarily stale."""
    global _last_spawn
    # When launched via launch.py, the orchestrator owns the scraper (espn_live) -- don't spawn a
    # competing one here. This avoids a startup race where the app's spawn grabbed the lock first
    # and starved launch.py's espn_live, leaving the (useless-for-live) REST poller in charge.
    if os.environ.get("CB_SCRAPER_MANAGED"):
        return
    if _lock_held():
        return                                # a scraper holds the lock -> definitely alive
    now = time.time()
    st = read_status()
    if st and (now - float(st.get("ts", 0))) < config.SCRAPER_STALE_SECONDS:
        return                                # a fresh heartbeat -> already running
    if now - _last_spawn < config.SCRAPER_STALE_SECONDS:
        return                                # gave a just-spawned poller time to boot
    _last_spawn = now
    _spawn()


def _spawn() -> None:
    """Launch the LIVE scraper (`python -m cheddarbowl.ingest.espn_live`) detached, logging to SCRAPER_LOG.
    (Not the legacy REST poller espn_scraper -- that can't see picks during a live draft.)"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    log = open(config.SCRAPER_LOG, "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW: run the scraper with no console window popping up (its output
        # already goes to SCRAPER_LOG). CREATE_NEW_PROCESS_GROUP keeps our Ctrl-C off it.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [sys.executable, "-u", "-m", "cheddarbowl.ingest.espn_live"],
            cwd=config.ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
    except Exception as e:  # noqa: BLE001 - never let a spawn failure crash the app
        log.write(f"[runner] spawn failed: {e}\n")
        log.flush()
