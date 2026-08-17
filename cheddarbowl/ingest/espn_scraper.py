"""ESPN fantasy scraper -- replaces the retired NFL.com scraper.

Polls the league's draft via the `espn-api` wrapper and writes ``draft_state.json`` in the exact
format the app already consumes, and (re)builds ``data/teams.csv``. Auth (``espn_s2`` + ``SWID``)
and the league id/year are read from ``data/espn_auth.json`` (local-only).

    python -m cheddarbowl.ingest.espn_scraper            # one-shot: write teams.csv + print draft summary
    python -m cheddarbowl.ingest.espn_scraper --poll     # the live loop the app spawns (filelock singleton)

The poll loop, heartbeat (``scraper_status.json``) and single-instance lock mirror the old NFL
scraper, so ``runner.py`` and the whole app/UI layer are unchanged -- only the data source moved.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time

from cheddarbowl import config


# --------------------------------------------------------------------------- auth / league
def _auth() -> dict:
    with open(config.ESPN_AUTH_JSON, encoding="utf-8") as f:
        a = json.load(f)
    # A mock draft lives in a different league; active_league.json overrides just the id, reusing
    # the same espn_s2/SWID cookies (you're a member of the mock too). Delete it to go back live.
    ov = config.active_override()
    if ov.get("league_id"):
        a["league_id"] = int(ov["league_id"])
    return a


def _make_league():
    from espn_api.football import League

    a = _auth()
    return League(league_id=a["league_id"], year=a["year"], espn_s2=a["espn_s2"], swid=a["swid"])


def _player_map(lg) -> dict:
    """{playerId: (name, pos, pro_team)} for filling pos/nfl-team on draft picks. Built once from
    the free-agent pool -- pre-draft that's every player, so drafted picks resolve from the cache
    even after they leave free agency."""
    pm: dict = {}
    try:
        for p in lg.free_agents(size=1500):
            pm[int(p.playerId)] = (p.name, p.position or "", p.proTeam or "")
    except Exception as e:  # noqa: BLE001 -- non-fatal; picks just fall back to "Player <id>"
        print(f"[warn] player map build failed: {e}")
    return pm


# --------------------------------------------------------------------------- teams -> teams.csv
def _logo_data_uri(url: str, cookies: dict) -> str:
    """ESPN custom team logos require auth (a plain <img src> gets 401), so fetch them server-side
    with the league cookies, shrink to ~96px, and inline as a small PNG data URI that renders in
    both the header's client-side <img> and st.image without cookies. '' if it can't be fetched."""
    if not url:
        return ""
    import base64
    import io

    import requests
    try:
        r = requests.get(url, cookies=cookies, timeout=15)
        if not (r.ok and r.content):
            return ""
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(r.content)).convert("RGBA")
            im.thumbnail((96, 96))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data, ct = buf.getvalue(), "image/png"
        except Exception:  # noqa: BLE001 -- resize is best-effort; fall back to the raw bytes
            data = r.content
            ct = (r.headers.get("Content-Type") or "image/png").split(";")[0]
        return f"data:{ct};base64,{base64.b64encode(data).decode()}"
    except Exception:  # noqa: BLE001
        return ""


def build_teams(lg) -> list[dict]:
    raw = lg.espn_request.get_league()
    # First name only -- matches the old app's compact header cards (unique in this league).
    members = {m["id"]: (m.get("firstName") or m.get("displayName") or "").strip()
               for m in raw.get("members", [])}
    a = _auth()
    cookies = {"espn_s2": a["espn_s2"], "SWID": a["swid"]}
    teams = []
    for t in raw.get("teams", []):
        owners = t.get("owners", []) or []
        teams.append({
            "team_id": t.get("id"),
            "team_name": t.get("name", f"Team {t.get('id')}"),
            "manager": members.get(owners[0], "") if owners else "",
            "co_manager": members.get(owners[1], "") if len(owners) > 1 else "",
            "avatar_url": _logo_data_uri(t.get("logo", "") or "", cookies),
        })
    return teams


def write_teams_csv(lg, path: str = config.TEAMS_CSV) -> int:
    teams = build_teams(lg)
    if not teams:
        # A failed/empty fetch must never clobber a good team map -- leave the existing file intact.
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic write: build the whole file under a temp name, then os.replace it in. A reader (the app)
    # sees either the old complete file or the new one -- never a half-written or 0-byte file. The old
    # direct open(path,"w") truncated first, so a kill mid-write (e.g. launch teardown taskkill) left
    # teams.csv empty and white-screen-crashed the app on next load.
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["team_id", "team_name", "manager", "co_manager", "avatar_url"])
        w.writeheader()
        w.writerows(teams)
    os.replace(tmp, path)
    return len(teams)


# --------------------------------------------------------------------------- draft -> state json
def _round_of(overall: int) -> int:
    return (overall - 1) // config.NUM_TEAMS + 1


def _team_on_clock(overall: int, order: list) -> "int | None":
    if not order:
        return None
    idx = (overall - 1) % config.NUM_TEAMS
    slot = idx + 1 if _round_of(overall) % 2 == 1 else config.NUM_TEAMS - idx
    return order[slot - 1] if 1 <= slot <= len(order) else None


def draft_state(lg, pmap: dict) -> dict:
    """Fetch the live draft and shape it like the NFL scraper's draft_state.json."""
    raw = lg.espn_request.get_league()                       # fresh fetch each call (this is the poll)
    dd = raw.get("draftDetail", {}) or {}
    picks_raw = dd.get("picks", []) or []
    # draft-slot order: prefer ESPN's configured pickOrder, else derive from round-1 picks.
    order = list(raw.get("settings", {}).get("draftSettings", {}).get("pickOrder", []) or [])

    rows = []
    for p in sorted(picks_raw, key=lambda x: x.get("overallPickNumber", 0)):
        overall = p.get("overallPickNumber")
        pid = int(p.get("playerId", 0))
        name, pos, nfl = pmap.get(pid, (f"Player {pid}", "", ""))
        rows.append({
            "pick": overall,
            "round": p.get("roundId", _round_of(overall or 1)),
            "team_id": p.get("teamId"),
            "player_id": str(pid),
            "player": name,
            "pos": pos,
            "nfl_team": nfl,
        })
    if not order and rows:
        order = [r["team_id"] for r in rows[: config.NUM_TEAMS]]

    nxt = len(rows) + 1
    return {
        "num_teams": config.NUM_TEAMS,
        "total_picks": len(rows),
        "draft_order": order,
        "on_the_clock": {"overall_pick": nxt, "round": _round_of(nxt),
                         "team_id": _team_on_clock(nxt, order)},
        "picks": rows,
    }


# --------------------------------------------------------------------------- io: state + heartbeat
def write_draft_state(state: dict, path: str = config.DRAFT_STATE_JSON) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _write_status(ok: bool, picks: int, error: "str | None" = None, stale: bool = False) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = config.SCRAPER_STATUS_JSON + ".tmp"
    payload = {"pid": os.getpid(), "ts": time.time(), "picks": picks,
               "ok": ok, "error": error, "stale": stale, "source": "espn"}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, config.SCRAPER_STATUS_JSON)


# --------------------------------------------------------------------------- CLI
def _cmd_poll() -> None:
    from filelock import FileLock, Timeout

    lock = FileLock(config.SCRAPER_LOCK)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        print("Another scraper already holds the lock; exiting.")
        return
    try:
        lg = _make_league()
        write_teams_csv(lg)                    # refresh the team map on start
        pmap = _player_map(lg)
        last_n = -1
        print(f"Polling ESPN draft every {config.POLL_SECONDS}s. Ctrl-C to stop.")
        while True:
            try:
                state = draft_state(lg, pmap)
                write_draft_state(state)
                n = state["total_picks"]
                _write_status(ok=True, picks=n, stale=(n == 0))   # 0 picks -> pre-draft
                if n != last_n:
                    print(f"  … {n} picks captured", end="\r")
                    last_n = n
                time.sleep(config.POLL_SECONDS)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:  # noqa: BLE001 -- keep the draft loop alive
                print(f"\n[warn] poll error: {e}; retrying in {config.POLL_SECONDS}s")
                _write_status(ok=False, picks=max(0, last_n), error=str(e))
                time.sleep(config.POLL_SECONDS)
    finally:
        lock.release()


def _cmd_once() -> None:
    lg = _make_league()
    n = write_teams_csv(lg)
    print(f"Wrote {n} teams -> {config.TEAMS_CSV}")
    pmap = _player_map(lg)
    print(f"Player map: {len(pmap)} players")
    state = draft_state(lg, pmap)
    print(f"Draft: {state['total_picks']} picks | order: {state['draft_order'][:config.NUM_TEAMS]}")
    for r in state["picks"][:8]:
        print(f"  #{r['pick']} team {r['team_id']}: {r['player']} {r['pos']}-{r['nfl_team']}")


if __name__ == "__main__":
    _cmd_poll() if "--poll" in sys.argv else _cmd_once()
