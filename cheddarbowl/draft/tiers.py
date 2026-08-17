"""Positional tiers: group players so the UI can show drop-off cliffs.

Within a position, sort by value and start a new tier wherever the point drop to the next
player is unusually large (a multiple of the position's typical gap). "Last player in a
tier" is where you either draft now or fall to a materially worse group.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def assign_tiers(players: pd.DataFrame, value_col: str = "proj_pts",
                 gap_factor: float = 1.8) -> pd.DataFrame:
    """Add a ``tier`` column (1 = best) computed per position via gap detection."""
    players = players.copy()
    players["tier"] = 1
    for pos, grp in players.groupby("pos"):
        g = grp.sort_values(value_col, ascending=False)
        vals = g[value_col].to_numpy(dtype=float)
        if len(vals) < 2:
            players.loc[g.index, "tier"] = 1
            continue
        gaps = -np.diff(vals)               # point drop between consecutive players
        pos_gaps = gaps[gaps > 0]
        thresh = gap_factor * float(np.median(pos_gaps)) if len(pos_gaps) else np.inf
        tiers = [1]
        cur = 1
        for gap in gaps:
            if gap > thresh:
                cur += 1
            tiers.append(cur)
        players.loc[g.index, "tier"] = tiers
    return players


def tier_context(players: pd.DataFrame, value_col: str = "proj_pts") -> pd.DataFrame:
    """Add ``tier_size`` and ``last_in_tier`` (bool) among currently-available players.

    Call on the available pool so "last in tier" reflects who's actually left. ``value_col`` is
    the lens metric (proj_pts / floor / ceiling) that defines the cliff edge.
    """
    players = players.copy()
    players["tier_size"] = 0
    players["last_in_tier"] = False
    col = value_col if value_col in players.columns else "proj_pts"
    for (_pos, _tier), grp in players.groupby(["pos", "tier"]):
        players.loc[grp.index, "tier_size"] = len(grp)
        # the lowest-value member of a multi-player tier is the cliff edge
        if len(grp) >= 1:
            worst = grp.sort_values(col).index[0]
            players.loc[worst, "last_in_tier"] = True
    return players


if __name__ == "__main__":
    from cheddarbowl.data_loader import load_players
    from cheddarbowl.draft.vbd import compute_vbd

    df, _, _ = compute_vbd(load_players())
    df = assign_tiers(df)
    for pos in ("RB", "WR", "TE", "QB"):
        g = df[df["pos"] == pos].sort_values("proj_pts", ascending=False).head(12)
        print(f"\n{pos} tiers:")
        print(g[["name", "proj_pts", "vob", "tier"]].to_string(index=False))
