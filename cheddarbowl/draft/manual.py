"""Manual-mode draft state: a scraper-independent fallback.

The user sets the draft order once (slot 1..N -> team_id) and then records picks by hand as
they happen. From that order + the ordered list of player_ids we reconstruct the exact same
``state`` dict the scraper/simulator produce, so every downstream panel works unchanged.

Persisted to config.MANUAL_DRAFT_JSON as {"order": [team_id,...], "picks": [player_id,...]}.
"""

from __future__ import annotations

import json
import os

import pandas as pd

from cheddarbowl import config


# --------------------------------------------------------------------------- persistence
def load_manual() -> dict:
    """{"order": [team_id...], "picks": [player_id...]}; empty scaffold if absent."""
    try:
        with open(config.MANUAL_DRAFT_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return {"order": [int(t) for t in d.get("order", [])],
                "picks": [str(p) for p in d.get("picks", [])]}
    except (OSError, ValueError):
        return {"order": [], "picks": []}


def save_manual(order: list[int], picks: list[str]) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = config.MANUAL_DRAFT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"order": [int(t) for t in order], "picks": [str(p) for p in picks]}, f, indent=2)
    os.replace(tmp, config.MANUAL_DRAFT_JSON)


# --------------------------------------------------------------------------- player universe
_players_cache: pd.DataFrame | None = None


def all_players() -> pd.DataFrame:
    """The player universe for the pick dropdown (player_id, name, pos, nfl_team).

    MUST be the same id space as the live board: a Manual pick joins the board by player_id, so this
    reads the engine's ESPN-keyed pool (load_players) -- NOT the retired NFL.com players file, whose
    ids don't overlap ESPN picks AT ALL (0/637), which silently broke Manual after the ESPN migration.
    """
    global _players_cache
    if _players_cache is None:
        from cheddarbowl.data_loader import load_players
        df = (load_players()[["player_id", "name", "pos", "team"]]
              .rename(columns={"team": "nfl_team"}))
        _players_cache = df.sort_values(["pos", "name"]).reset_index(drop=True)
    return _players_cache


def player_info() -> dict[str, dict]:
    """{player_id: {name, pos, team}} for building pick records."""
    df = all_players()
    return {r.player_id: {"name": r.name, "pos": r.pos, "team": r.nfl_team}
            for r in df.itertuples(index=False)}


def available_players(drafted_ids: list[str]) -> pd.DataFrame:
    """Undrafted players, ordered pos then name, for the entry dropdown."""
    taken = set(str(p) for p in drafted_ids)
    df = all_players()
    return df[~df["player_id"].isin(taken)]


# --------------------------------------------------------------------------- snake + state
def slot_on_clock(overall_pick: int, num_teams: int = config.NUM_TEAMS) -> int:
    """1-based draft slot for a given overall pick number (snake)."""
    idx = (overall_pick - 1) % num_teams
    rnd = (overall_pick - 1) // num_teams + 1
    return idx + 1 if rnd % 2 == 1 else num_teams - idx


def _team_for(overall_pick: int, order: list[int], num_teams: int) -> int | None:
    slot = slot_on_clock(overall_pick, num_teams)
    return order[slot - 1] if order and 1 <= slot <= len(order) else None


def build_state(order: list[int], picks: list[str], pinfo: dict[str, dict] | None = None) -> dict:
    """Reconstruct the standard draft ``state`` from a manual order + ordered player_ids."""
    pinfo = pinfo if pinfo is not None else player_info()
    n = config.NUM_TEAMS
    out_picks = []
    for i, pid in enumerate(picks):
        overall = i + 1
        info = pinfo.get(str(pid), {})
        out_picks.append({
            "pick": overall, "round": (overall - 1) // n + 1,
            "team_id": _team_for(overall, order, n), "player_id": str(pid),
            "player": info.get("name", str(pid)), "pos": info.get("pos", ""),
            "nfl_team": info.get("team", ""),
        })
    nxt = len(picks) + 1
    return {
        "num_teams": n, "total_picks": len(picks), "draft_order": list(order),
        "on_the_clock": {"overall_pick": nxt, "round": (nxt - 1) // n + 1,
                         "team_id": _team_for(nxt, order, n)},
        "picks": out_picks,
    }
