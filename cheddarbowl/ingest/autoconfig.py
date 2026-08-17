"""Auto-detect the league, the user's team, and ESPN auth from the app-managed browser -- so every
user needs zero manual setup: they just log into ESPN once and open their team/draft page, and this
reads everything over CDP.

Why CDP (not reading the normal browser's cookie store): modern Edge/Chrome encrypt cookies with
App-Bound Encryption, so an external app can't decrypt them. But we LAUNCH this browser ourselves
with --remote-debugging-port, and CDP hands us cookie values directly (incl. HttpOnly espn_s2),
with no decryption needed.

Writes:
  - data/espn_auth.json     {league_id, year, swid, espn_s2}   (scraper auth + REST)
  - data/active_league.json {league_id, my_team_id, name}      (overrides config for the UI)

    python -m cheddarbowl.ingest.autoconfig            # one-shot detect + write (browser must be on port 9222)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from cheddarbowl import config

CDP = "http://localhost:9222"
DEVTOOLS = CDP + "/json/list"


def _espn_tab_url() -> "str | None":
    """URL of an open ESPN fantasy-football tab that names a league (team or draft page)."""
    try:
        with urllib.request.urlopen(DEVTOOLS, timeout=5) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001 -- browser not up yet
        return None
    for t in data:
        u = t.get("url", "")
        if "fantasy.espn.com/football" in u and "leagueId=" in u:
            return u
    return None


def _qs(url: str, key: str) -> "str | None":
    m = re.search(rf"[?&]{key}=([^&#]+)", url)
    return m.group(1) if m else None


def _team_from_swid(league_id: int, season: int, cookies: dict, swid: str) -> "int | None":
    """The user's team_id, matched from their SWID against the league's owners. This is definitive and
    works even when the open page has no teamId in its URL (e.g. the /league page). Instant-mock
    leagues 404 here -> None, and detect() falls back to the URL's teamId."""
    try:
        import requests
        base = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
                f"{season}/segments/0/leagues/{league_id}")
        r = requests.get(base, params=[("view", "mTeam")], cookies=cookies, timeout=10)
        if not r.ok:
            return None
        mine = swid.strip("{}").upper()
        for t in r.json().get("teams", []):
            owners = [str(o).strip("{}").upper() for o in (t.get("owners") or [])]
            if mine in owners:
                return t.get("id")
    except Exception:  # noqa: BLE001
        return None
    return None


def _cookies() -> dict:
    """{espn_s2, SWID} from the managed browser via RAW CDP (Storage.getCookies over the browser-level
    debugger socket) -- HttpOnly included, no Playwright dependency. {} if the browser isn't ready yet;
    callers poll, so a transient failure is fine."""
    import asyncio

    import websockets

    async def _get() -> list:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=3) as r:
            wsurl = json.load(r)["webSocketDebuggerUrl"]
        async with websockets.connect(wsurl, max_size=None) as cdp:
            await cdp.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            while True:
                msg = json.loads(await cdp.recv())
                if msg.get("id") == 1:
                    return (msg.get("result", {}) or {}).get("cookies", []) or []

    try:
        cookies = asyncio.run(_get())
    except Exception:  # noqa: BLE001 -- browser not up / mid-navigation
        return {}
    out: dict = {}
    for c in cookies:
        if c.get("name") in ("espn_s2", "SWID") and "espn.com" in c.get("domain", ""):
            out[c["name"]] = c.get("value", "")
    return out


def detect(timeout: float = 180) -> "dict | None":
    """Poll until an ESPN team/draft tab is open AND logged in, then return the detected session:
    {league_id, team_id, season, espn_s2, swid}. None if it never appears within `timeout`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = _espn_tab_url()
        if url:
            ck = _cookies()
            league = _qs(url, "leagueId")
            if league and ck.get("espn_s2") and ck.get("SWID"):
                league_id = int(league)
                season_q = _qs(url, "seasonId")
                season = int(season_q) if season_q and season_q.isdigit() else config.SEASON
                cookies = {"espn_s2": ck["espn_s2"], "SWID": ck["SWID"]}
                # team by SWID first (works on any page incl. /league); URL teamId is the fallback.
                url_team = _qs(url, "teamId")
                team = _team_from_swid(league_id, season, cookies, ck["SWID"]) or \
                    (int(url_team) if url_team and url_team.isdigit() else None)
                return {
                    "league_id": league_id, "team_id": team, "season": season,
                    "espn_s2": ck["espn_s2"], "swid": ck["SWID"],
                }
        time.sleep(2)
    return None


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def write_config(sess: dict) -> None:
    """Persist a detected session into the files the rest of the app already reads."""
    _write_json(config.ESPN_AUTH_JSON, {
        "league_id": sess["league_id"], "year": sess["season"],
        "swid": sess["swid"], "espn_s2": sess["espn_s2"],
    })
    _write_json(config.ACTIVE_LEAGUE_JSON, {
        "league_id": sess["league_id"], "my_team_id": sess["team_id"],
        "name": "auto-detected from the browser",
    })


def main() -> None:
    print("Waiting for an ESPN team/draft tab (log in + open your team page)…", flush=True)
    sess = detect()
    if not sess:
        print("[autoconfig] no logged-in ESPN league tab found (is the browser on port 9222?).")
        return
    write_config(sess)
    tid = sess["team_id"]
    print(f"[autoconfig] league {sess['league_id']} · season {sess['season']} · "
          f"team {tid if tid is not None else '(unknown)'} · cookies OK "
          f"-> wrote {os.path.basename(config.ESPN_AUTH_JSON)} + "
          f"{os.path.basename(config.ACTIVE_LEAGUE_JSON)}")


if __name__ == "__main__":
    main()
