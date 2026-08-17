"""Headless screenshot of the running dashboard, for self-checking the design.

Writes a simulated draft_state to the live file, then screenshots the (Live-mode) page --
no browser-side mode toggle, so there's no race with the auto-refresh fragment.
Requires the Streamlit server on :8501 and `playwright install chromium`.

    python scripts/screenshot.py [out.png] [num_picks]
        26 -> you're on the clock ; 20 -> you pick next ; 15 -> a far ">>" jump
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl import config
from cheddarbowl.draft.simulate import simulate

URL = "http://localhost:8501"


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(config.DATA_DIR, "screenshot.png")
    num = int(sys.argv[2]) if len(sys.argv) > 2 else 26

    # Populate the live file BEFORE loading, so the first render is the state we want.
    json.dump(simulate(num), open(config.DRAFT_STATE_JSON, "w"))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 1320}, device_scale_factor=2)
        page.goto(URL, wait_until="load")
        page.wait_for_selector("div.cb-title", timeout=30000)          # theme rendered
        page.wait_for_selector(".cb-frole", timeout=60000)  # flow cards rendered
        page.wait_for_selector("text=Best available by position", timeout=60000)
        page.wait_for_selector("[data-testid='stDataFrame'] canvas", timeout=30000)
        page.get_by_text("Best available by position").scroll_into_view_if_needed()
        try:
            page.wait_for_load_state("networkidle", timeout=10000)  # headshots/avatars
        except Exception:
            pass
        page.wait_for_timeout(5000)  # canvas paint + fonts + glow
        page.screenshot(path=out, full_page=True)
        browser.close()
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
