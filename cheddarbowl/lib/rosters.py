"""Per-team roster construction from the draft picks + projections.

Turns a team's drafted players into an optimal starting lineup (by projected points) plus
bench, so the dashboard can show rosters and (later) forecast them.
"""

from __future__ import annotations

import pandas as pd

from cheddarbowl import config

# Starter slots in display order; FLEX drawn from FLEX_ELIGIBLE leftovers.
_STARTER_SLOTS = [("QB", "QB"), ("RB", "RB"), ("RB", "RB"), ("WR", "WR"), ("WR", "WR"),
                  ("FLEX", None), ("TE", "TE"), ("K", "K"), ("DST", "DST")]


def team_players(state: dict, team_id: int, players_all: pd.DataFrame) -> pd.DataFrame:
    """This team's drafted players with projected points (joined from the full board).

    Players not in the projection set (e.g. unmatched rookies) still appear, with 0 pts.
    """
    idx = players_all.set_index(players_all["player_id"].astype(str))
    rows = []
    for p in state.get("picks", []):
        if p["team_id"] != team_id:
            continue
        pid = str(p["player_id"])
        if pid in idx.index:
            r = idx.loc[pid]
            rows.append({"player_id": pid, "name": r["name"], "pos": r["pos"],
                         "team": r["team"], "proj_pts": float(r["proj_pts"]), "pick": p["pick"]})
        else:
            rows.append({"player_id": pid, "name": p["player"], "pos": p["pos"],
                         "team": p.get("nfl_team", ""), "proj_pts": 0.0, "pick": p["pick"]})
    return pd.DataFrame(rows, columns=["player_id", "name", "pos", "team", "proj_pts", "pick"])


def optimal_lineup(players: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Assign players to starter slots by best projected points; return (starters, bench).

    Each starters entry: {slot, player_id?, name?, pos?, team?, proj_pts?} — empty slots have
    name=None so the UI can render a blank/"—" line.
    """
    pool = players.sort_values("proj_pts", ascending=False).to_dict("records") if len(players) else []
    used: set[str] = set()

    def take(eligible_positions) -> dict | None:
        for r in pool:
            if r["player_id"] in used:
                continue
            if r["pos"] in eligible_positions:
                used.add(r["player_id"])
                return r
        return None

    starters = []
    for slot, pos in _STARTER_SLOTS:
        eligible = config.FLEX_ELIGIBLE if slot == "FLEX" else (pos,)
        pick = take(eligible)
        starters.append({"slot": slot, **(pick or {"name": None, "pos": "", "team": "",
                                                    "proj_pts": 0.0})})
    bench = [r for r in pool if r["player_id"] not in used]
    return starters, bench


def starter_points(starters: list[dict]) -> float:
    return round(sum(s.get("proj_pts", 0.0) or 0.0 for s in starters), 1)
