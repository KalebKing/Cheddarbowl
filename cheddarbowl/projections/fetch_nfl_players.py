"""Scrape the full NFL.com player universe (id, name, pos, team) for the crosswalk.

Iterates each position filter with pagination and writes data/nfl_players.csv. Run once
pre-draft. The stable ``playerId`` here is what live draft picks are matched against.
"""

from __future__ import annotations

import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl import config
from cheddarbowl.ingest.legacy.nfl_scraper import fetch, make_session
from cheddarbowl.ingest.legacy.parsing import parse_players_list, players_total_count

# NFL.com players-page position filter codes -> canonical position.
POS_CODES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 7: "K", 8: "DST"}
PAGE_SIZE = 25
_DELAY = 0.4  # polite delay between page requests


def _players_url(pos_code: int, offset: int) -> str:
    return (
        f"https://fantasy.nfl.com/league/{config.LEAGUE_ID}/players"
        f"?playerStatus=all&position={pos_code}"
        f"&statSeason={config.SEASON}&statType=seasonStats"
        f"&sort=pts&sortOrder=desc&offset={offset}"
    )


def fetch_all_players(session) -> list[dict]:
    seen: dict[str, dict] = {}
    for code, pos in POS_CODES.items():
        first = fetch(session, _players_url(code, 0))
        total = players_total_count(first) or 0
        pages = [first]
        for offset in range(PAGE_SIZE, total, PAGE_SIZE):
            time.sleep(_DELAY)
            pages.append(fetch(session, _players_url(code, offset)))
        n_before = len(seen)
        for html in pages:
            for ref in parse_players_list(html):
                # trust the position filter (page is single-position) over parsed em
                seen.setdefault(ref.player_id, {
                    "player_id": ref.player_id,
                    "name": ref.name,
                    "pos": pos,
                    "nfl_team": ref.nfl_team,
                })
        print(f"  {pos}: {total} listed, +{len(seen) - n_before} new (total {len(seen)})")
    return list(seen.values())


def main() -> None:
    session = make_session(config.COOKIES_JSON)
    print("Scraping NFL player universe...")
    players = fetch_all_players(session)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.NFL_PLAYERS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["player_id", "name", "pos", "nfl_team"])
        w.writeheader()
        w.writerows(sorted(players, key=lambda p: (p["pos"], p["name"])))
    print(f"Wrote {len(players)} players -> {config.NFL_PLAYERS_CSV}")


if __name__ == "__main__":
    main()
