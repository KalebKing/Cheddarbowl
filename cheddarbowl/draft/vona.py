"""VONA — Value Over Next Available.

For a player you could take now, VONA is how much better he is than the best player at his
position you can realistically expect to still be there at your next pick. A big VONA means
a cliff: take him now or lose that production. A small VONA means the position is deep or
quiet — you can wait and spend this pick elsewhere.

"Expected still available" uses the survival probabilities from pick_probability: a player
counts as likely-available if his p_survive clears SURVIVE_THRESHOLD.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SURVIVE_THRESHOLD = 0.5  # a player is "expected available" if p_survive >= this


def add_vona(avail: pd.DataFrame) -> pd.DataFrame:
    """Add ``vona`` (in VOB points) and ``next_available`` (name) per player.

    Requires columns: pos, vob, name, p_survive (from add_pick_probability).

    For each player, VONA is measured against the best OTHER same-position player expected to
    survive to your next pick (highest vob strictly below his). Within a position sorted by vob
    descending, that "next available" is found by a binary search into the survivor vobs -- O(n
    log n) per position instead of the old O(n^2) row-by-row mask (output is identical; this is
    ~100x faster on a full board, which matters because Live mode rescoring runs every few sec).
    """
    avail = avail.copy()
    if avail.empty:
        avail["vona"] = []
        avail["next_available"] = []
        return avail

    vona_vals: dict = {}
    next_name: dict = {}
    for _pos, grp in avail.groupby("pos"):
        g = grp.sort_values("vob", ascending=False)
        idxs = g.index.to_numpy()
        vob = g["vob"].to_numpy(dtype=float)
        names = g["name"].to_numpy(dtype=object)
        surv = (g["p_survive"] >= SURVIVE_THRESHOLD).to_numpy()
        sv_asc = vob[surv][::-1]        # survivor vobs, ascending
        sn_asc = names[surv][::-1]
        # first survivor with vob strictly below each player's vob = the one just under the
        # searchsorted insertion point (side="left" keeps equal-vob survivors excluded, matching
        # the old strict `pool["vob"] < row["vob"]` -- so a player never counts himself).
        j = np.searchsorted(sv_asc, vob, side="left") - 1
        for k in range(len(g)):
            if j[k] >= 0:
                vona_vals[idxs[k]] = float(vob[k] - sv_asc[j[k]])
                next_name[idxs[k]] = sn_asc[j[k]]
            else:
                # nobody worth having survives -> falling to replacement level (vob 0)
                vona_vals[idxs[k]] = float(max(vob[k], 0.0))
                next_name[idxs[k]] = None
    avail["vona"] = avail.index.map(vona_vals)
    avail["next_available"] = avail.index.map(next_name)
    return avail
