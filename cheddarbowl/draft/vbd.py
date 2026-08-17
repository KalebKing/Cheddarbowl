"""Value-Based Drafting: make points comparable across positions.

A QB's 320 projected points is not worth an RB's 320 — what matters is a player's value
*above the replaceable starter at his position*. We compute a per-position baseline (the
last startable player once FLEX demand is allocated) and subtract it: ``vob`` (value over
baseline) is the cross-position currency the rest of the engine runs on.
"""

from __future__ import annotations

import pandas as pd

from cheddarbowl import config


def effective_demand(players: pd.DataFrame) -> dict[str, int]:
    """League-wide startable count per position, with FLEX apportioned by value.

    Base starters come from config.STARTERS x NUM_TEAMS. The FLEX slots are handed to the
    best remaining FLEX-eligible players (RB/WR in this league), so RB/WR baselines sit
    deeper than TE's.
    """
    demand = config.league_starter_demand()  # {QB:12, RB:24, WR:24, TE:12, K:12, DST:12}

    # Candidates for FLEX = players ranked beyond their position's base starters.
    pool: list[tuple[str, float]] = []
    for pos in config.FLEX_ELIGIBLE:
        pp = players.loc[players["pos"] == pos, "proj_pts"].sort_values(ascending=False)
        for pts in pp.iloc[demand[pos]:]:
            pool.append((pos, float(pts)))
    pool.sort(key=lambda x: x[1], reverse=True)
    for pos, _ in pool[: config.FLEX_DEMAND]:
        demand[pos] += 1
    return demand


def compute_vbd(players: pd.DataFrame,
                metric: str = "proj_pts") -> tuple[pd.DataFrame, dict[str, float], dict[str, int]]:
    """Add ``vbd_baseline``, ``vob``, ``vbd_rank``. Returns (players, baselines, demand).

    ``metric`` is the projection column value is measured on — the "value lens": ``proj_pts``
    (default), ``floor`` (safe/reliable), or ``ceiling`` (upside). Roster demand / FLEX structure
    stays anchored on proj_pts (same league shape); only the baselines + vob shift to the lens.
    A player missing the lens column (some lack floor/ceiling) falls back to proj_pts so no one
    drops out of the board.
    """
    players = players.copy()
    demand = effective_demand(players)
    vals = players[metric].fillna(players["proj_pts"]) if metric != "proj_pts" else players["proj_pts"]

    baselines: dict[str, float] = {}
    for pos in config.POSITIONS:
        pts = vals[players["pos"] == pos].sort_values(ascending=False).values
        if len(pts) == 0:
            baselines[pos] = 0.0
            continue
        n = demand.get(pos, 0)
        idx = min(max(n, 1), len(pts)) - 1  # last starter (n is 1-based)
        baselines[pos] = float(pts[idx])

    players["vbd_baseline"] = players["pos"].map(baselines)
    players["vob"] = vals - players["vbd_baseline"]
    players = players.sort_values("vob", ascending=False).reset_index(drop=True)
    players["vbd_rank"] = range(1, len(players) + 1)
    return players, baselines, demand


if __name__ == "__main__":
    from cheddarbowl.data_loader import load_players

    df, baselines, demand = compute_vbd(load_players())
    print("Effective demand (incl. FLEX):", demand)
    print("Baselines (last-starter pts):", {k: round(v, 1) for k, v in baselines.items()})
    print("\nTop 20 by VOB (cross-position value):")
    cols = ["vbd_rank", "name", "pos", "team", "proj_pts", "vob", "adp"]
    print(df[cols].head(20).to_string(index=False))
