"""Unify projections + id crosswalk (+ live draft state) into one player table.

This is the single source of truth the strategy engine and dashboard read: one row per
NFL-rostered player, keyed by NFL ``player_id`` so it joins directly to live draft picks.
"""

from __future__ import annotations

import json
import os

import pandas as pd

from cheddarbowl import config

# Columns carried through from the ffanalytics projection table.
_PROJ_COLS = {
    "points": "proj_pts",
    "floor": "floor",
    "ceiling": "ceiling",
    "sd_pts": "sd_pts",
    "adp": "adp",
    "adp_sd": "adp_sd",
    "pos_rank": "ffa_pos_rank",
    "tier": "ffa_tier",
    "points_vor": "ffa_vor",
}


def load_players() -> pd.DataFrame:
    """One row per matched player, keyed by NFL ``player_id``.

    Columns: player_id, name, pos, team, proj_pts, floor, ceiling, sd_pts, adp,
    ffa_pos_rank, ffa_tier, ffa_vor.
    """
    proj = pd.read_csv(config.PROJECTIONS_CSV, dtype={"id": str})
    xwalk = pd.read_csv(config.CROSSWALK_CSV, dtype={"player_id": str, "proj_id": str})

    id2nfl = dict(zip(xwalk["proj_id"], xwalk["player_id"]))
    id2name = dict(zip(xwalk["proj_id"], xwalk["player"]))

    proj = proj[proj["id"].isin(id2nfl)].copy()
    out = pd.DataFrame({
        "player_id": proj["id"].map(id2nfl),
        "name": proj["id"].map(id2name),
        "pos": proj["position"],
        "team": proj["team"],
    })
    for src, dst in _PROJ_COLS.items():
        out[dst] = pd.to_numeric(proj[src], errors="coerce") if src in proj else pd.NA

    out = out.dropna(subset=["proj_pts"]).drop_duplicates("player_id")
    out = out.sort_values("proj_pts", ascending=False).reset_index(drop=True)
    out["overall_rank"] = range(1, len(out) + 1)
    return out


def load_headshots(path: str = config.HEADSHOTS_CSV) -> dict[str, str]:
    """{player_id: headshot_url} from the pre-warmed CSV; empty dict if not built yet."""
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, dtype=str)
    return {pid: url for pid, url in zip(df["player_id"], df["headshot_url"].fillna("")) if url}


_last_good_teams: "dict | None" = None   # last cleanly-parsed team map (defensive fallback)


def load_teams(path: str = config.TEAMS_CSV) -> dict[int, dict]:
    """{team_id: {team_name, manager, co_manager, avatar_url}}; empty if not built.

    Defensive: a truncated/corrupt teams.csv (e.g. a writer killed mid-write leaving 0 bytes) must
    NOT crash the app -- pd.read_csv raises EmptyDataError on an empty file. On a bad read we hold
    the last good map, or {} if we never had one (the app then shows TBD names + a rebuild prompt).
    """
    global _last_good_teams
    if not os.path.exists(path):
        return {}
    try:
        # fillna("") so empty CSV cells become "" not NaN -- NaN is *truthy*, so a missing manager
        # would otherwise slip past `manager or "Team N"` fallbacks and render as the string "nan".
        df = pd.read_csv(path, dtype=str).fillna("")
        teams = {int(r["team_id"]): {
            "team_name": r.get("team_name", ""),
            "manager": r.get("manager", ""),
            "co_manager": r.get("co_manager", ""),
            "avatar_url": r.get("avatar_url", ""),
        } for _, r in df.iterrows()}
    except (OSError, ValueError, KeyError):
        return _last_good_teams or {}
    _last_good_teams = teams
    return teams


_last_good_state: "dict | None" = None   # last cleanly-parsed draft state (defensive fallback)


def load_draft_state(path: str = config.DRAFT_STATE_JSON) -> dict:
    """Read draft_state.json (as written by the scraper); empty scaffold if absent.

    Defensive by design: the scraper rewrites this file every couple seconds while the app reads
    it on a timer. Writes are atomic (os.replace), so a torn read shouldn't happen -- but a
    genuinely corrupt file (a crash mid-write, a disk glitch) must NEVER crash the app during a
    live draft. On a bad parse we hold the last state we read cleanly, so the board stays put
    instead of the whole page erroring out. An ABSENT file is treated as an intentional reset
    (new draft) and returns the blank scaffold, matching the launcher deleting it on start.
    """
    global _last_good_state
    scaffold = {"num_teams": config.NUM_TEAMS, "total_picks": 0, "draft_order": [],
                "on_the_clock": {}, "picks": []}
    if not os.path.exists(path):
        return scaffold
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return _last_good_state or scaffold
    _last_good_state = state
    return state


def apply_draft_state(players: pd.DataFrame, state: dict) -> pd.DataFrame:
    """Annotate the player table with draft status from a draft_state dict.

    Adds: drafted (bool), drafted_by (team_id or NA). Picks reference NFL player_id, so
    the join is exact.
    """
    picks = state.get("picks", [])
    by_id = {str(p["player_id"]): p["team_id"] for p in picks}
    players = players.copy()
    players["drafted_by"] = players["player_id"].map(by_id)
    players["drafted"] = players["drafted_by"].notna()
    return players


def available(players: pd.DataFrame) -> pd.DataFrame:
    """Just the undrafted players (requires apply_draft_state first)."""
    return players[~players.get("drafted", False)]


if __name__ == "__main__":
    df = load_players()
    print(f"Loaded {len(df)} players.")
    print(df.groupby("pos").size().to_dict())
    print("\nTop 15 overall (half-PPR VBD comes later; this is raw proj_pts):")
    cols = ["overall_rank", "name", "pos", "team", "proj_pts", "adp", "ffa_tier"]
    print(df[cols].head(15).to_string(index=False))
