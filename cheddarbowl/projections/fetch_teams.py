"""Capture league team metadata -> data/teams.csv (run once pre-draft).

team_id, team_name, manager, co_manager, avatar_url — from the owners page. Used by the
dashboard to label picks and show whose turn it is with the team's photo.

    python scripts/fetch_teams.py
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl import config
from cheddarbowl.ingest.legacy.nfl_scraper import get_owners, make_session


def main() -> None:
    owners = get_owners(make_session(config.COOKIES_JSON))
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.TEAMS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["team_id", "team_name", "manager", "co_manager", "avatar_url"])
        w.writeheader()
        for o in owners:
            w.writerow({
                "team_id": o.team_id, "team_name": o.team_name, "manager": o.manager,
                "co_manager": o.co_manager, "avatar_url": o.avatar_url,
            })
    print(f"Wrote {len(owners)} teams -> {config.TEAMS_CSV}")
    for o in owners:
        print(f"  {o.team_id:>2}. {o.team_name}  —  {o.manager}"
              f"{' / ' + o.co_manager if o.co_manager else ''}  "
              f"{'[photo]' if o.avatar_url else '[no photo]'}")


if __name__ == "__main__":
    main()
