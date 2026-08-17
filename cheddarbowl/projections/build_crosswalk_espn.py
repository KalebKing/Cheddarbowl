"""Match ffanalytics projections to ESPN playerIds -> data/player_crosswalk.csv (ESPN edition).

Same contract as the old NFL crosswalk (columns: player_id, proj_id, player, pos, nfl_team,
points, adp) -- but `player_id` is now the **ESPN** playerId, so live ESPN draft picks join to
projected value. `load_players()` is unchanged; it just keys the pool on whatever id is here.

Reuses the matching logic from build_crosswalk.py (normalized name + position, team as a
tiebreaker; DST matched by team). Only the player source changes: the ESPN free-agent universe
(pre-draft that's every draftable player) instead of the NFL.com players page.

    python scripts/build_crosswalk_espn.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from cheddarbowl import config
from cheddarbowl.projections.build_crosswalk import build_index, load_projection_identities, match, norm_name

# ESPN position label -> our projection position label (ESPN calls team defense "D/ST").
_POS = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "D/ST": "DST", "DST": "DST"}

# ESPN team abbrev -> the canonical form the projections use (only the ones that differ).
# Team is just a tiebreaker for same-name players, so this only needs the real divergences.
_ESPN_TEAM = {"WSH": "WAS", "JAX": "JAX", "LAR": "LAR", "LAC": "LAC", "LV": "LV"}


def espn_players() -> pd.DataFrame:
    """The ESPN draftable universe -> columns build_index expects (player_id, name, nfl_team, pos)."""
    from espn_api.football import League

    a = json.load(open(config.ESPN_AUTH_JSON, encoding="utf-8"))
    lg = League(league_id=a["league_id"], year=a["year"], espn_s2=a["espn_s2"], swid=a["swid"])
    rows = []
    for p in lg.free_agents(size=2000):
        pos = _POS.get(p.position, str(p.position))
        team = _ESPN_TEAM.get((p.proTeam or "").upper(), (p.proTeam or "").upper())
        rows.append({"player_id": str(p.playerId), "name": p.name, "nfl_team": team, "pos": pos})
    return pd.DataFrame(rows)


def load_manual_overrides() -> dict:
    """{normalized proj_name: ESPN player_id} hand fixes from crosswalk_manual.csv (rows with a
    blank player_id are documentation-only -- players confirmed absent from ESPN's pool)."""
    if not os.path.exists(config.CROSSWALK_MANUAL_CSV):
        return {}
    m = pd.read_csv(config.CROSSWALK_MANUAL_CSV, dtype={"player_id": str})
    return {norm_name(r["proj_name"]): str(r["player_id"]).strip()
            for _, r in m.iterrows()
            if pd.notna(r.get("player_id")) and str(r["player_id"]).strip()}


# ESPN team-logo abbrev quirks (for DST headshots); most match the projection abbrevs.
_LOGO_TEAM = {"WAS": "wsh"}


def write_headshots(matched: pd.DataFrame) -> int:
    """headshots.csv keyed on ESPN ids. Skill/K use ESPN's deterministic headshot URL; DST use the
    team logo. Both load anonymously (verified 200), so the app's <img>/st.image show them."""
    rows = []
    for _, r in matched.iterrows():
        pid, pos = str(r["player_id"]), str(r["pos"])
        if pos == "DST":
            team = _LOGO_TEAM.get(str(r["nfl_team"]).upper(), str(r["nfl_team"]).lower())
            url = f"https://a.espncdn.com/i/teamlogos/nfl/500/{team}.png"
        else:
            url = f"https://a.espncdn.com/i/headshots/nfl/players/full/{pid}.png"
        rows.append({"player_id": pid, "headshot_url": url})
    if os.path.exists(config.HEADSHOTS_CSV):
        os.replace(config.HEADSHOTS_CSV, config.HEADSHOTS_CSV.replace(".csv", "_nfl_backup.csv"))
    pd.DataFrame(rows).to_csv(config.HEADSHOTS_CSV, index=False)
    return len(rows)


def main() -> None:
    proj = load_projection_identities()
    espn = espn_players()
    print(f"ESPN pool: {len(espn)} players | positions: {espn['pos'].value_counts().to_dict()}")

    idx, idx_li, idx_dst = build_index(espn)
    matched, unmatched = match(proj, idx, idx_li, idx_dst)

    # Apply hand fixes to whatever the auto-matcher missed (e.g. FBs ESPN tags RB, not TE).
    overrides = load_manual_overrides()
    already = set(matched["proj_id"])
    extra = [{"player_id": eid, "proj_id": p["proj_id"], "player": p["proj_name"], "pos": p["pos"],
              "nfl_team": p["proj_team"], "points": p["points"], "adp": p["adp"]}
             for _, p in proj.iterrows()
             if p["proj_id"] not in already and (eid := overrides.get(p["norm"]))]
    if extra:
        matched = pd.concat([matched, pd.DataFrame(extra)], ignore_index=True)
        unmatched = unmatched[~unmatched["proj_name"].map(norm_name).isin(overrides)]
        print(f"Applied {len(extra)} manual override(s) from {os.path.basename(config.CROSSWALK_MANUAL_CSV)}")

    if os.path.exists(config.CROSSWALK_CSV):
        os.replace(config.CROSSWALK_CSV, config.CROSSWALK_CSV.replace(".csv", "_nfl_backup.csv"))
    matched.to_csv(config.CROSSWALK_CSV, index=False)

    total = len(proj)
    print(f"Matched {len(matched)}/{total} projections to ESPN ids ({100 * len(matched) / total:.1f}%)")
    print(f"Wrote {config.CROSSWALK_CSV}")
    print(f"Wrote {write_headshots(matched)} headshots -> {config.HEADSHOTS_CSV}")
    if len(unmatched):
        top = unmatched.sort_values("points", ascending=False).head(25)
        print(f"\nTop unmatched (of {len(unmatched)}) -- highest projected first:")
        print(top.to_string(index=False))


if __name__ == "__main__":
    main()
