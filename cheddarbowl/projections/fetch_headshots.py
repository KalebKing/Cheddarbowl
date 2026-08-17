"""Pre-warm player headshot URLs -> data/headshots.csv (run once pre-draft).

For each projectable player we fetch their NFL.com card page and extract the CDN headshot
URL (team logo for DST). The dashboard then reads this CSV and renders images instantly,
with nothing fetching during the live draft.

    python scripts/fetch_headshots.py
"""

from __future__ import annotations

import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl import config
from cheddarbowl.ingest.legacy.nfl_scraper import fetch, make_session
from cheddarbowl.ingest.legacy.parsing import parse_headshot
from cheddarbowl.data_loader import load_players

_DELAY = 0.3


def _card_url(player_id: str) -> str:
    return f"https://fantasy.nfl.com/players/card?leagueId={config.LEAGUE_ID}&playerId={player_id}"


def main() -> None:
    players = load_players()  # the ~566 matched, projectable players (what the UI can show)
    session = make_session(config.COOKIES_JSON)

    # Resume-friendly: keep any already-resolved rows.
    have: dict[str, str] = {}
    if os.path.exists(config.HEADSHOTS_CSV):
        import pandas as pd
        prev = pd.read_csv(config.HEADSHOTS_CSV, dtype=str)
        have = dict(zip(prev["player_id"], prev["headshot_url"].fillna("")))

    rows: list[dict] = []
    todo = [(pid, nm) for pid, nm in zip(players["player_id"], players["name"])
            if not have.get(pid)]
    print(f"{len(players)} players; {len(have)} cached; fetching {len(todo)}...")

    ok = 0
    for i, (pid, name) in enumerate(todo, 1):
        try:
            url = parse_headshot(fetch(session, _card_url(pid)))
        except Exception as e:  # noqa: BLE001 - skip a bad card, keep going
            url = None
            print(f"  [warn] {name} ({pid}): {e}")
        have[pid] = url or ""
        ok += bool(url)
        if i % 25 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} resolved ({ok} with image)")
        time.sleep(_DELAY)

    for pid in players["player_id"]:
        rows.append({"player_id": pid, "headshot_url": have.get(pid, "")})
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.HEADSHOTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["player_id", "headshot_url"])
        w.writeheader()
        w.writerows(rows)
    filled = sum(1 for r in rows if r["headshot_url"])
    print(f"Wrote {len(rows)} rows ({filled} with headshots) -> {config.HEADSHOTS_CSV}")


if __name__ == "__main__":
    main()
