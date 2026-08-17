"""Single orchestrator for the Cheddarbowl draft assistant -- ONE command launches everything and
collapses the old four windows (2 cmd + 2 browser) into one browser window + an invisible app:

  1. app-managed browser (Chromium, own debug port + isolated profile) at the user's league page
  2. auto-config: detect league/team/cookies from that browser and write the config  [scraper.autoconfig]
  3. live scraper (espn_live) + Streamlit dashboard, both as HIDDEN background processes
  4. the dashboard as a chromeless --app window, side-by-side with the draft
  5. run until the browser is closed, then shut the background processes down

    python launch.py

Notes:
  - The browser is launched DIRECTLY (not via Playwright) so ESPN's SPA doesn't detect automation.
  - Step 3 will swap the installed-Edge lookup for a bundled Chromium; nothing else changes.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

from cheddarbowl import config

CDP_PORT = 9222
PROFILE = config.BROWSER_PROFILE_DIR       # persistent login profile (browser-agnostic), under runtime/
WS_CAPTURE = config.WS_CAPTURE_JSONL
LOG_PATH = config.LAUNCH_LOG
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW -> no console pop-ups

# Installed-Chromium fallbacks, used only if the bundled Chromium isn't available.
_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _bundled_chromium() -> "str | None":
    """A Chromium shipped alongside the app in a local ./chromium folder, if present. Launched DIRECTLY
    (subprocess, below) -- never via automation -- so it's an ordinary Chromium (no navigator.webdriver)
    and ESPN renders normally. The league build ships WITHOUT this (relies on installed Edge/Chrome);
    a future public/Mac build can drop a Chromium here to become fully self-contained. No Playwright."""
    for exe in ("chrome.exe", "chrome", "chromium"):
        p = os.path.join(config.ROOT, "chromium", exe)
        if os.path.exists(p):
            return p
    return None


def _log(msg: str, fh=None) -> None:
    line = f"[launch] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def _find_browser() -> "str | None":
    return _bundled_chromium() or next((p for p in _BROWSERS if os.path.exists(p)), None)


def _free_port(preferred: int = 8501) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _initial_url() -> str:
    """The user's league page if we've configured one before, else the fantasy home (first run)."""
    try:
        ov = json.load(open(config.ACTIVE_LEAGUE_JSON, encoding="utf-8"))
        if ov.get("league_id"):
            return f"https://fantasy.espn.com/football/league?leagueId={ov['league_id']}"
    except (OSError, ValueError):
        pass
    return "https://fantasy.espn.com/football/"


def _http_ok(url: str, timeout: float = 2) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def _cdp_up() -> bool:
    return _http_ok(f"http://localhost:{CDP_PORT}/json/version")


def _kill_prior() -> None:
    """Reap any orphaned app process (a prior run's streamlit/scraper) before we start, so a zombie
    can't keep holding the port and serving stale code -- closing the window doesn't always reap the
    child processes. Matches this install dir (works for dev AND the packaged copy); excludes the
    helper's own PID. Windows-only, best-effort."""
    if os.name != "nt":
        return
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
        f"$_.CommandLine -like '*{config.ROOT}*' -and "
        "($_.CommandLine -like '*streamlit*apps*draft.py*' -or "
        "$_.CommandLine -like '*cheddarbowl.ingest.espn_live*') -and $_.ProcessId -ne $PID } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       creationflags=_NO_WINDOW, capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _reset_state() -> None:
    # NB: scraper.lock is deliberately NOT in this list. The filelock is the single-instance guard;
    # deleting it while a prior scraper is still alive would let a duplicate grab a fresh lock and run
    # concurrently (the spawn-storm that worsened the live-draft outage). A leftover lock FILE is
    # harmless -- filelock re-acquires the OS lock on it -- and the holder releases it when it exits.
    for f in (config.DRAFT_STATE_JSON, WS_CAPTURE, config.PICK_OVERRIDES_JSON,
              config.HOST_TOKEN_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def _lan_ip() -> str:
    """Best-effort LAN IP for the shareable URL (no packets are actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _write_host_token() -> str:
    """Write a fresh random host token. The app grants full (host) controls only to the session
    whose URL carries ?host=<this token> -- i.e. the window this launcher opens. Everyone else on
    the wifi who opens the plain URL is a read-only viewer."""
    import secrets
    tok = secrets.token_urlsafe(9)
    try:
        with open(config.HOST_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


def main() -> None:
    browser = _find_browser()
    if not browser:
        _log("No Chromium browser (Edge/Chrome) found. Install Edge or Chrome.")
        return
    os.makedirs(config.DATA_DIR, exist_ok=True)
    _kill_prior()          # reap any orphaned instance so we bind the port + load current code
    fh = open(LOG_PATH, "a", encoding="utf-8")
    _reset_state()
    host_token = _write_host_token()   # this launcher's window is the host; LAN viewers get plain URL
    # We (the orchestrator) own the scraper -- tell the dashboard subprocess NOT to spawn its own,
    # so its runner.ensure_running() can't win the lock race against our espn_live. Children inherit.
    os.environ["CB_SCRAPER_MANAGED"] = "1"
    # This launcher starts a BRAND-NEW draft (state files cleared just above), so the scraper must NOT
    # recover prior picks. Explicit + robust even if a _reset_state() delete failed on a locked file.
    # (A mid-draft scraper restart, by contrast, recovers by default -- see espn_live._run.)
    os.environ["CB_FRESH_DRAFT"] = "1"

    # dashboard start + window helpers (used at different points for returning vs first-run users)
    port = _free_port(8501)
    py = sys.executable
    dash_url = f"http://localhost:{port}"
    host_url = f"{dash_url}?host={host_token}"          # YOUR window: full controls
    lan_url = f"http://{_lan_ip()}:{port}"              # share THIS with leaguemates: read-only viewer
    common = dict(stdout=fh, stderr=fh, stdin=subprocess.DEVNULL, cwd=config.ROOT,
                  creationflags=_NO_WINDOW)

    def _start_app():
        _log(f"starting dashboard on :{port}… (share {lan_url} on your wifi)", fh)
        return subprocess.Popen([py, "-m", "streamlit", "run", "apps/draft.py",
                                 "--server.port", str(port), "--server.headless", "true",
                                 # bind all interfaces so people on the same wifi can reach it
                                 "--server.address", "0.0.0.0",
                                 "--browser.gatherUsageStats", "false",
                                 # Draft day: no file-watcher. An accidental edit mid-draft must not
                                 # hot-reload and desync modules (that caused a live crash before).
                                 "--server.fileWatcherType", "none"], **common)

    def _open_window():
        for _ in range(90):
            if _http_ok(dash_url):
                break
            time.sleep(1)
        else:
            _log("dashboard didn't come up in time (opening the window anyway).", fh)
        _log(f"opening YOUR dashboard window (host): {dash_url}", fh)
        _log(f"SHARE WITH LEAGUEMATES (same wifi, read-only): {lan_url}", fh)
        subprocess.Popen([browser, f"--app={host_url}", f"--user-data-dir={PROFILE}"])

    # 1) launch the app-managed browser at the league page (direct launch, own profile + debug port)
    _log(f"launching browser: {os.path.basename(browser)}", fh)
    subprocess.Popen([browser, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE}",
                      "--no-first-run", "--no-default-browser-check", _initial_url()])

    # 2) RETURNING user (config already exists): boot the dashboard NOW, overlapping its ~4s cold
    #    start with the browser launch + login + auto-config so the window appears fast. First run
    #    has no valid per-user config yet, so it must wait for auto-config before booting the app.
    returning = os.path.exists(config.ACTIVE_LEAGUE_JSON)
    app = _start_app() if returning else None
    scraper = None

    try:
        # 3) wait for the browser's debug port (needed by auto-config + the scraper)
        for _ in range(30):
            if _cdp_up():
                break
            time.sleep(1)
        else:
            _log("browser debug port never came up; aborting.", fh)
            return

        # 4) returning user: pop the dashboard window as soon as it serves (don't wait on auto-config)
        if app is not None:
            _open_window()

        # 5) auto-config: detect league/team + refresh the rotating cookie for the scraper
        from cheddarbowl.ingest import autoconfig
        _log("detecting league + login…", fh)
        sess = autoconfig.detect(timeout=300)
        if sess:
            autoconfig.write_config(sess)
            _log(f"configured: league {sess['league_id']} · team {sess['team_id']} · season {sess['season']}", fh)
        else:
            _log("could not auto-detect the league; continuing with existing config.", fh)

        # 6) first run: config now exists -> boot the dashboard and open its window
        if app is None:
            app = _start_app()
            _open_window()

        # 7) start the live scraper (needs the fresh cookies from auto-config)
        _log("starting live scraper…", fh)
        scraper = subprocess.Popen([py, "-u", "-m", "cheddarbowl.ingest.espn_live"], **common)

        # 8) run until the browser is closed
        _log("up and running — draft in the ESPN window, watch the assistant window. Close the browser to quit.", fh)
        _log(f"➜ To let leaguemates follow along on the SAME wifi, share this URL: {lan_url}", fh)
        _log("  (they pick their team in the sidebar; they're read-only. Allow it through the "
             "Windows firewall if prompted.)", fh)
        while _cdp_up():
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        _log("browser closed — shutting down background processes.", fh)
        for proc in (scraper, app):
            if proc is None:
                continue
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        for proc in (scraper, app):
            if proc is None:
                continue
            try:
                proc.wait(timeout=6)
            except Exception:  # noqa: BLE001 -- didn't die: force-kill the tree (venv child holds lock)
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   creationflags=_NO_WINDOW, capture_output=True)
        # Drop the host token so a later dev run isn't treated as a viewer. Leave scraper.lock alone:
        # the scraper releases its own lock via _run's finally; a leftover file is harmless.
        try:
            os.remove(config.HOST_TOKEN_FILE)
        except OSError:
            pass
        _log("shut down.", fh)


if __name__ == "__main__":
    main()
