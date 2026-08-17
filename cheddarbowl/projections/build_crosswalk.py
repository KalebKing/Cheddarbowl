"""Match ffanalytics projections to NFL.com playerIds -> data/player_crosswalk.csv.

Projections and NFL.com use different id systems, so we match on normalized name +
position (team as a tiebreaker). The resulting crosswalk lets the strategy engine join
live draft picks (NFL playerId) to projected value.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from cheddarbowl import config

# ffanalytics team abbrev -> NFL.com abbrev (only the ones that differ).
TEAM_CANON = {
    "GBP": "GB", "KCC": "KC", "NEP": "NE", "NOS": "NO", "SFO": "SF",
    "TBB": "TB", "LVR": "LV", "JAC": "JAX",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str) -> str:
    """Lowercase, drop punctuation and generational suffixes, collapse spaces."""
    s = str(name).lower()
    s = s.replace(".", "").replace("'", "").replace("-", " ")
    s = re.sub(r"[^a-z\s]", " ", s)
    parts = [p for p in s.split() if p not in _SUFFIXES]
    return " ".join(parts)


def canon_team(team: str) -> str:
    t = str(team).upper()
    return TEAM_CANON.get(t, t)


def load_projection_identities() -> pd.DataFrame:
    # id must stay a string: DST ids are zero-padded (e.g. "0532") and would otherwise
    # be parsed as ints, breaking the join to the projections table downstream.
    df = pd.read_csv(config.PROJECTIONS_CSV, dtype={"id": str})
    df["proj_name"] = (df["first_name"].fillna("") + " " + df["last_name"].fillna("")).str.strip()
    df["pos"] = df["position"].astype(str)
    df["norm"] = df["proj_name"].map(norm_name)
    df["team_c"] = df["team"].map(canon_team)
    return df[["id", "proj_name", "pos", "team_c", "norm", "points", "adp"]].rename(
        columns={"id": "proj_id", "team_c": "proj_team"})


def _last_initial(norm: str) -> tuple[str, str] | None:
    """(last_name, first_initial) key for nickname-tolerant fallback matching."""
    parts = norm.split()
    if len(parts) < 2:
        return None
    return (parts[-1], parts[0][0])


def build_index(nfl: pd.DataFrame) -> tuple[dict, dict, dict]:
    nfl = nfl.copy()
    nfl["norm"] = nfl["name"].map(norm_name)
    nfl["team_c"] = nfl["nfl_team"].map(canon_team)
    idx: dict[tuple[str, str], list[dict]] = {}       # (pos, full norm name)
    idx_li: dict[tuple, list[dict]] = {}              # (pos, last, first initial)
    idx_dst: dict[str, dict] = {}                     # team -> DST entry
    for _, r in nfl.iterrows():
        entry = {"player_id": str(r["player_id"]), "name": r["name"], "team": r["team_c"]}
        idx.setdefault((r["pos"], r["norm"]), []).append(entry)
        li = _last_initial(r["norm"])
        if li:
            idx_li.setdefault((r["pos"], *li), []).append(entry)
        if r["pos"] == "DST":
            idx_dst[r["team_c"]] = entry
    return idx, idx_li, idx_dst


def _pick(cands: list[dict], proj_team: str) -> dict | None:
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:  # disambiguate by team
        same = [c for c in cands if c["team"] == proj_team]
        return same[0] if len(same) == 1 else None
    return None


def match(proj: pd.DataFrame, idx: dict, idx_li: dict, idx_dst: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, unmatched = [], []
    for _, p in proj.iterrows():
        if p["pos"] == "DST":
            # Team defenses have different names across systems; match on team.
            chosen = idx_dst.get(p["proj_team"])
        else:
            # Tier 1: exact normalized name + position.
            chosen = _pick(idx.get((p["pos"], p["norm"]), []), p["proj_team"])
            # Tier 2: nickname-tolerant (last name + first initial), team-disambiguated.
            if not chosen:
                li = _last_initial(p["norm"])
                if li:
                    chosen = _pick(idx_li.get((p["pos"], *li), []), p["proj_team"])
        if chosen:
            rows.append({
                "player_id": chosen["player_id"], "proj_id": p["proj_id"],
                "player": chosen["name"], "pos": p["pos"], "nfl_team": chosen["team"],
                "points": p["points"], "adp": p["adp"],
            })
        else:
            unmatched.append({"proj_name": p["proj_name"], "pos": p["pos"],
                              "team": p["proj_team"], "points": p["points"]})
    return pd.DataFrame(rows), pd.DataFrame(unmatched)


def main() -> None:
    proj = load_projection_identities()
    nfl = pd.read_csv(config.NFL_PLAYERS_CSV, dtype={"player_id": str})
    idx, idx_li, idx_dst = build_index(nfl)
    matched, unmatched = match(proj, idx, idx_li, idx_dst)
    matched.to_csv(config.CROSSWALK_CSV, index=False)

    total = len(proj)
    print(f"Matched {len(matched)}/{total} projections to NFL ids "
          f"({100*len(matched)/total:.1f}%)")
    print(f"Wrote {config.CROSSWALK_CSV}")
    if len(unmatched):
        # Show the highest-value misses first -- those are the ones worth fixing.
        top = unmatched.sort_values("points", ascending=False).head(20)
        print(f"\nTop unmatched (of {len(unmatched)}):")
        print(top.to_string(index=False))


if __name__ == "__main__":
    main()
