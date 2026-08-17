"""In-live pick corrections (manual overrides that don't require leaving Live mode).

The user can fix a pick the websocket scraper missed or mis-recorded while STAYING in Live: the fix
is stored here, and the scraper applies it on top of its board on every write (see
``Draft.apply_overrides`` in scraper/espn_live.py), so the correction sticks and the app sees it on
its next refresh. The scraper is the single applier -- this module is just the shared store + a small
helper to overlay overrides onto a state dict for anything that reads the file directly.

Format on disk (config.PICK_OVERRIDES_JSON):  {"<overall#>": {"team_id": int, "player_id": "str"}}
"""
from __future__ import annotations

import json
import os

from cheddarbowl import config


def _path() -> str:
    """The overrides file path. Resolved via getattr with a DATA_DIR fallback so a Streamlit
    hot-reload that left a STALE `config` in memory (one predating PICK_OVERRIDES_JSON) can't crash
    this with AttributeError -- DATA_DIR has always existed, so the fallback is always available."""
    return getattr(config, "PICK_OVERRIDES_JSON", None) or os.path.join(
        config.DATA_DIR, "pick_overrides.json")


def load() -> dict[int, dict]:
    """{overall#: {"team_id": int, "player_id": str}}; {} if absent/corrupt (never raises)."""
    try:
        with open(_path(), encoding="utf-8") as f:
            raw = json.load(f)
        out: dict[int, dict] = {}
        for k, v in raw.items():
            out[int(k)] = {"team_id": int(v["team_id"]), "player_id": str(v["player_id"])}
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save(d: dict[int, dict]) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = _path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in sorted(d.items())}, f, indent=2)
    os.replace(tmp, path)


def set_override(overall: int, team_id: int, player_id: str) -> None:
    d = load()
    d[int(overall)] = {"team_id": int(team_id), "player_id": str(player_id)}
    _save(d)


def remove_override(overall: int) -> None:
    d = load()
    if int(overall) in d:
        del d[int(overall)]
        _save(d)


def clear() -> None:
    try:
        os.remove(_path())
    except OSError:
        pass


def apply_to_state(state: dict, pinfo: "dict | None" = None) -> dict:
    """Return a copy of `state` with overrides overlaid onto its picks (for readers that consume the
    file directly). Resolves name/pos/team from the ESPN player pool. The scraper does its own
    application against its live board; this keeps a plain state dict consistent with the overrides."""
    ov = load()
    if not ov:
        return state
    if pinfo is None:
        from cheddarbowl.draft.manual import player_info
        pinfo = player_info()
    n = int(state.get("num_teams", config.NUM_TEAMS))
    by_overall = {int(p["pick"]): dict(p) for p in state.get("picks", [])}
    for overall, o in ov.items():
        info = pinfo.get(str(o["player_id"]), {})
        by_overall[overall] = {
            "pick": overall, "round": (overall - 1) // n + 1,
            "team_id": o["team_id"], "player_id": str(o["player_id"]),
            "player": info.get("name", str(o["player_id"])),
            "pos": info.get("pos", ""), "nfl_team": info.get("team", ""),
        }
    picks = [by_overall[k] for k in sorted(by_overall)]
    out = dict(state)
    out["picks"] = picks
    out["total_picks"] = len(picks)
    return out
