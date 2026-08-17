"""One-time capture harness: launch a HEADFUL Chromium with a persistent profile, let you log into
ESPN and open a (mock/practice) draft board, and record every websocket frame the draft page
exchanges -> data/ws_capture.jsonl. We analyze that file to learn the live-pick feed's URL + message
format, then build the real scraper on top of the SAME logged-in profile.

    python -m cheddarbowl.ingest.legacy.capture_ws            # opens the practice-draft board; log in if prompted

Nothing is clicked for you; it only *observes* the page's websockets (native Playwright, no JS hook).
"""
from __future__ import annotations

import json
import os
import time

from cheddarbowl import config
from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.join(config.DATA_DIR, "pw_profile")   # persists login across runs
CAPTURE = os.path.join(config.DATA_DIR, "ws_capture.jsonl")
LID = 1335304409
BOARD_URL = (f"https://fantasy.espn.com/football/draft?leagueId={LID}"
             f"&seasonId=2026&teamId=5&memberId=%7BD92A0683-520D-44EF-AFB0-FDD65BA85D2A%7D")


def _rec(fh, obj: dict) -> None:
    obj["t"] = round(time.time(), 3)
    fh.write(json.dumps(obj) + "\n")
    fh.flush()


def _payload(data) -> tuple[str, str]:
    """(kind, text) — text frames verbatim (truncated), binary marked with length."""
    if isinstance(data, (bytes, bytearray)):
        return "binary", f"<{len(data)} bytes>"
    return "text", str(data)[:2000]


def main() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fh = open(CAPTURE, "a", encoding="utf-8")
    print(f"Capture file: {CAPTURE}")
    print(f"Profile dir : {PROFILE_DIR}")

    def wire(page) -> None:
        def on_ws(ws) -> None:
            _rec(fh, {"ev": "ws-open", "url": ws.url})
            print(f"[ws] OPEN {ws.url}")
            ws.on("framereceived", lambda p: (
                _rec(fh, {"ev": "recv", "url": ws.url, **dict(zip(("kind", "data"), _payload(p)))})))
            ws.on("framesent", lambda p: (
                _rec(fh, {"ev": "sent", "url": ws.url, **dict(zip(("kind", "data"), _payload(p)))})))
            ws.on("close", lambda: _rec(fh, {"ev": "ws-close", "url": ws.url}))
        page.on("websocket", on_ws)

    with sync_playwright() as p:
        # channel="msedge" drives the real installed Microsoft Edge (Chromium engine) instead of
        # Playwright's bundled Chromium -- renders ESPN's SPA more reliably / less bot friction.
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, channel="msedge", no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
        ctx.on("page", wire)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        wire(pg)
        try:
            pg.goto(BOARD_URL, wait_until="commit", timeout=60000)   # don't block on the heavy SPA
        except Exception as e:  # noqa: BLE001
            print(f"[nav] {e}")
        _rec(fh, {"ev": "note", "msg": "board opened; waiting for login + picks"})
        print("\n>>> Log into ESPN in the window if prompted, then open/observe the draft.")
        print(">>> Leave this running. Ctrl-C here (or close the window) when done capturing.\n")
        n = 0
        try:
            while True:
                time.sleep(3)
                n += 1
                if n % 10 == 0:
                    print(f"  … still capturing ({n*3}s)")
        except KeyboardInterrupt:
            print("\nStopping capture.")
        finally:
            fh.close()
            ctx.close()


if __name__ == "__main__":
    main()
