"""LIVE ESPN draft scraper via the draft room's own websocket (the real-time feed the REST API does
NOT expose during a draft). Attaches over CDP to a normally-launched Edge (--remote-debugging-port
=9222), hooks the blob web worker carrying the socket, parses the text-line protocol, and writes
draft_state.json + the heartbeat exactly like espn_scraper -- so the whole app/UI layer is unchanged.

Protocol (server->client text frames):
    SELECTED <teamId> <playerId> <lineupSlotId> [ {memberSWID} ]   -- a pick was made
    SELECTING <teamId> <clockMs>                                   -- team is on the clock
    CLOCK <?> <msRemaining> <teamId>                               -- clock tick
    AUTOSUGGEST <playerId>                                         -- ESPN's suggestion
D/ST are negative playerIds.

Exact pick numbers: the socket doesn't stamp an overall pick number, and SELECTED frames can arrive
slightly out of order. So we DON'T number by arrival -- instead each team's Nth SELECTED is its
round-N pick, and overall# = snake(slot, round) using an authoritative slot order:
    1) REST pickOrder (the real league; instant mocks 404 -> skip),
    2) else the live SELECTING clock-sequence (teams go on the clock in true draft-slot order),
    3) else first-seen order of SELECTED (last resort).
This is immune to frame reordering. Picks dedupe by playerId, so replays never double-count.

Robustness: a discovery loop re-scans CDP targets every ~2s and (re)attaches to any worker/page that
appears -- surviving worker recycling and FOLLOWING you into a brand-new draft. Draft targets are
recognized by the frames they emit, not their URL (new-draft workers report a blank URL).

    python -m cheddarbowl.ingest.espn_live                 # attach to the live draft in the debug Edge
    python -m cheddarbowl.ingest.espn_live --replay FILE    # offline: re-run a ws_capture.jsonl through the parser
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request

import websockets

from cheddarbowl import config

CAPTURE = config.WS_CAPTURE_JSONL
DEVTOOLS = "http://localhost:9222/json/list"
RECONCILE_SECONDS = 20    # how often to cross-check the board against ESPN's authoritative REST detail
_VERBS = ("SELECTED", "SELECTING", "CLOCK", "AUTOSUGGEST", "SELECT")
_SKIP = ("recaptcha", "google", "doubleclick", "criteo", "rubicon", "adtrafficquality",
         "imrworldwide", "safeframe", "gstatic", "registerdisney", "chartbeat")


def _snake(slot: int, rnd: int, n: int) -> int:
    """Overall pick number for draft `slot` (1-based) in `rnd` (1-based) of an n-team snake."""
    return (rnd - 1) * n + (slot if rnd % 2 == 1 else n - slot + 1)


class Draft:
    def __init__(self, log: bool = True) -> None:
        self.names = self._load_names()
        self.n = config.NUM_TEAMS
        self.slot_order: list[int] = []       # authoritative slot -> team_id
        self.order_locked = False             # True once REST gave us the real order
        self.team_round: dict[int, int] = {}  # team_id -> picks made so far (== their current round)
        self.by_overall: dict[int, dict] = {} # overall pick# -> pick record
        self.drafted: set[str] = set()
        self.on_clock_team: "int | None" = None
        self._cap = open(CAPTURE, "a", encoding="utf-8") if log else None

    # -- setup -------------------------------------------------------------
    @staticmethod
    def _load_names() -> dict:
        import csv
        d = {}
        try:
            with open(config.CROSSWALK_CSV, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    d[str(r["player_id"])] = (r["player"], r["pos"], r["nfl_team"])
        except OSError:
            pass
        return d

    def set_order(self, order: list) -> None:
        order = [int(t) for t in (order or []) if t]
        if len(order) >= self.n:
            self.slot_order = order[: self.n]
            self.order_locked = True

    def _slot_of(self, team: int) -> int:
        """1-based draft slot for a team; learn it from clock/pick order if REST didn't lock one."""
        if team not in self.slot_order:
            if not self.order_locked and len(self.slot_order) < self.n:
                self.slot_order.append(team)
            else:
                return len(self.slot_order) + 1     # unknown late arrival -> best-effort tail slot
        return self.slot_order.index(team) + 1

    def _resolve(self, pid: str):
        return self.names.get(pid) or self.names.get(pid.lstrip("-")) or (f"Player {pid}", "", "")

    # -- frame handling ----------------------------------------------------
    def handle(self, data: str):
        """Return the new pick record on a fresh SELECTED, the string 'clock' on SELECTING (state
        changed but nothing to announce), or None. Callers print only for a dict so a SELECTING can't
        re-announce the previous pick."""
        p = data.split()
        if not p or p[0] not in _VERBS:
            return None
        if self._cap:
            self._cap.write(json.dumps({"ev": "recv", "data": data[:5000],
                                        "t": round(time.time(), 3)}) + "\n")
            self._cap.flush()
        if p[0] == "SELECTING" and len(p) >= 2:
            t = int(p[1])
            self.on_clock_team = t
            self._slot_of(t)                        # clock order is the truest slot signal
            return "clock"
        if p[0] == "SELECTED" and len(p) >= 3:
            return self.add_pick(int(p[1]), p[2])
        return None

    def add_pick(self, team_id: int, pid: str):
        """Return the pick record if it's new, else None (dedupe by playerId)."""
        if pid in self.drafted:
            return None
        self.drafted.add(pid)
        rnd = self.team_round.get(team_id, 0) + 1
        self.team_round[team_id] = rnd
        slot = self._slot_of(team_id)
        overall = _snake(slot, rnd, self.n)
        name, pos, nfl = self._resolve(pid)
        rec = {"pick": overall, "round": rnd, "team_id": team_id,
               "player_id": pid, "player": name, "pos": pos, "nfl_team": nfl}
        self.by_overall[overall] = rec
        return rec

    # -- reconciliation ----------------------------------------------------
    def _resync_counters(self) -> None:
        """Rebuild the dedupe set + per-team round counts from by_overall. Called after a reconcile
        edit so the websocket path keeps numbering later picks correctly (a team's round == how many
        picks it holds)."""
        self.drafted = {str(r["player_id"]) for r in self.by_overall.values()}
        tr: dict = {}
        for r in self.by_overall.values():
            tr[r["team_id"]] = tr.get(r["team_id"], 0) + 1
        self.team_round = tr

    def apply_overrides(self) -> int:
        """Overlay the user's in-live pick corrections (config.PICK_OVERRIDES_JSON) on top of the
        websocket board. Called on every write() so a hand-fixed pick sticks even if a later frame
        disagrees -- the override wins by exact overall#. Lets the user correct a missed/wrong pick
        without leaving Live mode. Returns the number applied (0 if none). Read is fully guarded."""
        try:
            with open(config.PICK_OVERRIDES_JSON, encoding="utf-8") as f:
                ov = json.load(f)
        except (OSError, ValueError):
            return 0
        if not isinstance(ov, dict) or not ov:
            return 0
        changed = 0
        for k, v in ov.items():
            try:
                overall, team, pid = int(k), int(v["team_id"]), str(v["player_id"])
            except (TypeError, ValueError, KeyError):
                continue
            cur = self.by_overall.get(overall)
            if cur and str(cur["player_id"]) == pid and int(cur["team_id"]) == team:
                continue
            name, pos, nfl = self._resolve(pid)
            self.by_overall[overall] = {"pick": overall, "round": (overall - 1) // self.n + 1,
                                        "team_id": team, "player_id": pid,
                                        "player": name, "pos": pos, "nfl_team": nfl}
            changed += 1
        if changed:
            self._resync_counters()
        return changed

    def reconcile(self, rest: dict) -> int:
        """Heal the board against ESPN's authoritative REST draft detail.

        `rest` maps overall# -> (team_id, player_id) for picks REST reports with a REAL playerId. A
        websocket-*missed* pick is worse than a gap: it shifts every later pick for that team by a
        round (each team's Nth SELECTED is numbered round N). REST carries the exact overallPickNumber,
        so we overwrite our record for the trustworthy CONTIGUOUS prefix 1..M and re-derive counters --
        which fixes both the missing pick and the shift behind it.

        No-op when REST hasn't populated playerIds yet (all -1 during a live draft) -> the websocket
        stays the only source, exactly as before. Only ever acts on the prefix that's contiguous from
        pick 1, never on gappy/partial REST. Returns the number of picks changed.
        """
        if not rest:
            return 0
        m = 0
        while (m + 1) in rest:          # trust only the run of picks contiguous from #1
            m += 1
        changed = 0
        for overall in range(1, m + 1):
            team, pid = rest[overall]
            cur = self.by_overall.get(overall)
            if cur and str(cur["player_id"]) == str(pid) and int(cur["team_id"]) == int(team):
                continue                # already correct
            name, pos, nfl = self._resolve(str(pid))
            self.by_overall[overall] = {"pick": overall, "round": (overall - 1) // self.n + 1,
                                        "team_id": int(team), "player_id": str(pid),
                                        "player": name, "pos": pos, "nfl_team": nfl}
            changed += 1
        if changed:
            self._resync_counters()
        return changed

    # -- output ------------------------------------------------------------
    def seed_from_log(self) -> int:
        """Crash recovery: replay a prior ws_capture.jsonl to rebuild the board, since ESPN won't
        replay picks to a fresh attach. A clean draft-day start deletes the log first (see
        run-draft.bat), so this only fires when re-running the scraper mid-draft."""
        if not os.path.exists(CAPTURE):
            return 0
        cap, self._cap = self._cap, None            # don't re-log frames while replaying them
        try:
            for ln in open(CAPTURE, encoding="utf-8"):
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if o.get("ev") == "recv":
                    self.handle(str(o.get("data", "")))
        finally:
            self._cap = cap
        return len(self.by_overall)

    def picks_sorted(self) -> list[dict]:
        return [self.by_overall[k] for k in sorted(self.by_overall)]

    def state(self) -> dict:
        picks = self.picks_sorted()
        nxt = len(picks) + 1
        ct = self.on_clock_team
        if ct is None and self.slot_order:
            idx = (nxt - 1) % self.n
            rnd = (nxt - 1) // self.n + 1
            slot = idx + 1 if rnd % 2 == 1 else self.n - idx
            ct = self.slot_order[slot - 1] if 1 <= slot <= len(self.slot_order) else None
        return {"num_teams": self.n, "total_picks": len(picks), "draft_order": self.slot_order,
                "on_the_clock": {"overall_pick": nxt, "round": (nxt - 1) // self.n + 1, "team_id": ct},
                "picks": picks}

    @staticmethod
    def _atomic_write(path: str, obj) -> None:
        """Atomic JSON write, retrying the replace. On Windows os.replace raises WinError 5 if the app
        has the destination open for reading at that instant -- transient, so retry briefly and give
        up quietly rather than bubbling up (which would detach the socket)."""
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        for i in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.03 * (i + 1))
        try:
            os.remove(tmp)
        except OSError:
            pass

    def write(self) -> None:
        self.apply_overrides()   # in-live manual corrections win, re-applied on every write
        self._atomic_write(config.DRAFT_STATE_JSON, self.state())
        self.status(ok=True)

    def seed_from_state_file(self) -> int:
        """Crash recovery from draft_state.json (authoritative, bounded to one draft).

        This avoids replaying stale websocket captures from an older draft session.
        """
        if not os.path.exists(config.DRAFT_STATE_JSON):
            return 0
        try:
            with open(config.DRAFT_STATE_JSON, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return 0

        order = [int(t) for t in (st.get("draft_order") or []) if t]
        if len(order) >= self.n:
            self.slot_order = order[: self.n]
            self.order_locked = True

        recovered = 0
        for p in sorted(st.get("picks", []) or [], key=lambda x: int(x.get("pick", 0) or 0)):
            try:
                overall = int(p.get("pick", 0) or 0)
                rnd = int(p.get("round", 0) or 0)
                team_id = int(p.get("team_id", 0) or 0)
                pid = str(p.get("player_id", ""))
                if overall <= 0 or rnd <= 0 or team_id <= 0 or not pid:
                    continue
            except (TypeError, ValueError):
                continue

            name = str(p.get("player") or self._resolve(pid)[0])
            pos = str(p.get("pos") or self._resolve(pid)[1])
            nfl = str(p.get("nfl_team") or self._resolve(pid)[2])
            self.by_overall[overall] = {
                "pick": overall,
                "round": rnd,
                "team_id": team_id,
                "player_id": pid,
                "player": name,
                "pos": pos,
                "nfl_team": nfl,
            }
            self.team_round[team_id] = max(self.team_round.get(team_id, 0), rnd)
            self.drafted.add(pid)
            recovered += 1

        otc = (st.get("on_the_clock") or {}).get("team_id")
        try:
            self.on_clock_team = int(otc) if otc is not None else None
        except (TypeError, ValueError):
            self.on_clock_team = None
        return recovered

    def status(self, ok: bool, error: "str | None" = None) -> None:
        n = len(self.by_overall)
        self._atomic_write(config.SCRAPER_STATUS_JSON,
                           {"pid": os.getpid(), "ts": time.time(), "picks": n, "ok": ok,
                            "error": error, "stale": (n == 0), "source": "espn-live"})


# --------------------------------------------------------------------------- REST authoritative order
def _draft_league_id() -> "str | None":
    """leagueId of the OPEN draft page (the draft we're actually capturing), which may differ from the
    configured league for a mock. Using it means the REST pickOrder matches THIS draft, so the user's
    slot is right from pick 1 instead of forming over round 1 off the live clock."""
    import re
    try:
        with urllib.request.urlopen(DEVTOOLS, timeout=3) as r:
            data = json.load(r)
        for t in data:
            u = t.get("url", "")
            if "/football/draft" in u and "leagueId=" in u:
                m = re.search(r"leagueId=(\d+)", u)
                if m:
                    return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _rest_order(league_id: "str | None" = None) -> list:
    """A league's configured pickOrder (slot->team). Empty for instant mocks (404) or on error.
    Defaults to the configured league; pass the ACTUAL draft's league so the order matches it."""
    try:
        import requests
        a = json.load(open(config.ESPN_AUTH_JSON, encoding="utf-8"))
        lid = league_id or config.LEAGUE_ID
        base = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
                f"{a['year']}/segments/0/leagues/{lid}")
        r = requests.get(base, params=[("view", "mSettings")],
                         cookies={"espn_s2": a["espn_s2"], "SWID": a["swid"]}, timeout=10)
        if r.ok:
            return r.json().get("settings", {}).get("draftSettings", {}).get("pickOrder", []) or []
    except Exception:  # noqa: BLE001
        pass
    return []


def _rest_picks(league_id: "str | None") -> dict:
    """Authoritative picks from ESPN's REST draft detail for a SPECIFIC league: {overall# ->
    (team_id, player_id)}, only for picks that carry a real playerId. Returns {} on any error, an
    instant-mock 404, or before REST populates playerIds (they read -1 during a live draft).

    league_id is REQUIRED and must be the ACTUAL draft's league (from the open draft page). We never
    fall back to the configured league here: reconciling a mock against your real league's picks
    would corrupt the board. -1/0 are the 'not yet made' placeholders; real D/ST ids are large
    negatives, so we exclude only exactly -1 and 0."""
    if not league_id:
        return {}
    try:
        import requests
        a = json.load(open(config.ESPN_AUTH_JSON, encoding="utf-8"))
        base = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
                f"{a['year']}/segments/0/leagues/{league_id}")
        r = requests.get(base, params=[("view", "mDraftDetail")],
                         cookies={"espn_s2": a["espn_s2"], "SWID": a["swid"]}, timeout=10)
        if not r.ok:
            return {}
        out: dict = {}
        for p in (r.json().get("draftDetail", {}) or {}).get("picks", []) or []:
            overall = p.get("overallPickNumber")
            pid = p.get("playerId")
            team = p.get("teamId")
            if overall and team and pid not in (None, -1, 0):
                out[int(overall)] = (int(team), str(int(pid)))
        return out
    except Exception:  # noqa: BLE001
        return {}


def _entry_pid(e: dict) -> "int | None":
    """Player id from an mRoster entry, robust to ESPN's key nesting: top-level `playerId`, else
    `playerPoolEntry.id`, else `playerPoolEntry.player.id`. So a populated roster is never missed
    just because the id sits one level deeper than expected."""
    pid = e.get("playerId")
    if pid in (None, 0):
        pp = e.get("playerPoolEntry") or {}
        pid = pp.get("id") or (pp.get("player") or {}).get("id")
    try:
        return int(pid) if pid not in (None, 0) else None
    except (TypeError, ValueError):
        return None


def _roster_snapshot(league_id: "str | None") -> tuple:
    """({team_id: set(player_id)}, total_raw_entries) from ESPN's mRoster view for a SPECIFIC league.

    Powers the roster-diff DIAGNOSTIC: unlike mDraftDetail (which reads -1 until the draft finalizes),
    rosters are *believed* to populate live as players are drafted -- this measures whether that's
    actually true. `total_raw_entries` is the count of roster slots BEFORE id extraction, so the
    diagnostic can tell an empty roster (entries=0 -> not live) apart from a parse miss (entries>0 but
    pids=0). ({}, 0) on any error; actual-draft league only (never the config fallback)."""
    if not league_id:
        return {}, 0
    try:
        import requests
        a = json.load(open(config.ESPN_AUTH_JSON, encoding="utf-8"))
        base = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
                f"{a['year']}/segments/0/leagues/{league_id}")
        r = requests.get(base, params=[("view", "mRoster")],
                         cookies={"espn_s2": a["espn_s2"], "SWID": a["swid"]}, timeout=10)
        if not r.ok:
            return {}, 0
        out: dict = {}
        raw = 0
        for t in r.json().get("teams", []) or []:
            tid = t.get("id")
            entries = ((t.get("roster") or {}).get("entries")) or []
            raw += len(entries)
            pids = {str(p) for p in (_entry_pid(e) for e in entries) if p is not None}
            if tid is not None:
                out[int(tid)] = pids
        return out, raw
    except Exception:  # noqa: BLE001
        return {}, 0


def _rest_rosters(league_id: "str | None") -> dict:
    """{team_id: set(player_id)} from mRoster (thin wrapper over _roster_snapshot for consumers that
    don't need the raw-entry count)."""
    return _roster_snapshot(league_id)[0]


def _try_lock_order(draft: "Draft") -> None:
    """Lock the authoritative pickOrder for the ACTUAL draft's league, if we can and haven't already.
    Only ever from the open draft page (never the configured league) so a mock never inherits the
    wrong order. No-op once locked or once picks have started (so we never renumber mid-draft)."""
    if draft.order_locked or draft.by_overall:
        return
    lid = _draft_league_id()
    if not lid:
        return
    order = _rest_order(lid)
    if order:
        draft.set_order(order)
        print(f"locked authoritative pickOrder (league {lid}): {order}", flush=True)


# --------------------------------------------------------------------------- CDP capture
def _targets() -> list[dict]:
    try:
        with urllib.request.urlopen(DEVTOOLS, timeout=5) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in data:
        if t.get("type") not in ("worker", "page") or not t.get("webSocketDebuggerUrl"):
            continue
        if any(s in t.get("url", "").lower() for s in _SKIP):
            continue
        out.append(t)
    return out


async def _pump(tgt: dict, draft: Draft, attached: set) -> None:
    tid = tgt["id"]
    attached.add(tid)
    try:
        # Keepalive pings are ESSENTIAL, not optional. When the ESPN draft room reloads, its
        # worker (carrying the draft socket) is recycled; the OLD CDP debugger transport to that
        # worker often half-closes WITHOUT a clean FIN. With ping_interval=None (no keepalive),
        # `async for raw in cdp` would then block forever -- no frames, no exception -- so this
        # target stayed in `attached` and the discovery loop never re-attached to the NEW worker.
        # That is the silent "alive but capturing 0 picks" failure from the live draft. With
        # keepalive on, a dead transport raises within ~ping_timeout, _pump exits, the finally
        # drops `tid` from `attached`, and _discover re-attaches to the live worker on its next tick.
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None,
                                       ping_interval=20, ping_timeout=20,
                                       close_timeout=5) as cdp:
            await cdp.send(json.dumps({"id": 1, "method": "Network.enable"}))
            print(f"[attach] {tgt.get('type')} {tgt.get('url','')[:45] or '(blank-url worker)'}", flush=True)
            async for raw in cdp:
                msg = json.loads(raw)
                if msg.get("method") == "Network.webSocketFrameReceived":
                    r = msg["params"].get("response", {}) or {}
                    if r.get("opcode") in (1, 2):
                        try:
                            res = draft.handle(str(r.get("payloadData", "")))
                            if res:                          # dict = new pick, "clock" = on-the-clock
                                if isinstance(res, dict):
                                    print(f"  #{res['pick']:>3} R{res['round']} team {res['team_id']:>2}"
                                          f"  {res['player']} {res['pos']}", flush=True)
                                draft.write()
                        except Exception as he:  # noqa: BLE001 -- a bad frame must not drop the socket
                            print(f"[frame err] {str(he)[:80]}", flush=True)
    except Exception as e:  # noqa: BLE001 -- target died/recycled; discovery re-attaches
        print(f"[detach] {tid[:8]}: {str(e)[:60]}", flush=True)
    finally:
        attached.discard(tid)


async def _discover(draft: Draft) -> None:
    attached: set = set()
    while True:
        # The discovery loop is the scraper's heartbeat -- it MUST NOT die mid-draft. A transient
        # error (a target-scan hiccup, a write glitch, a bad REST response) is logged and the loop
        # keeps going; without this guard such an error would unwind to _run, release the lock, and
        # the scraper would exit -- going blind until the failsafe nudges you to Manual.
        try:
            _try_lock_order(draft)      # lock the real order once the draft room is open (pre-pick-1)
            for t in _targets():
                if t["id"] not in attached:
                    asyncio.create_task(_pump(t, draft, attached))
            # Keep draft_state.json warm even when no new frames arrive or when a startup write
            # transiently failed on Windows.
            draft.write()
        except Exception as e:  # noqa: BLE001 -- resilience: never let one bad tick stop discovery
            print(f"[discover err] {str(e)[:100]}", flush=True)
        await asyncio.sleep(2)


async def _reconcile_loop(draft: Draft) -> None:
    """Safety net for missed picks: periodically cross-check the websocket-built board against ESPN's
    authoritative REST draft detail and heal any discrepancies. A dropped socket/worker gap can lose
    a SELECTED frame, and the websocket can't backfill it -- REST can. Runs on a slow cadence (the
    REST call is heavier + rate-limited) and in a thread so it never stalls frame capture. Guarded so
    a bad tick can't kill the scraper; a no-op whenever REST hasn't populated (the common live case)."""
    await asyncio.sleep(RECONCILE_SECONDS)     # let the websocket establish + take the early picks
    loop = asyncio.get_event_loop()
    last_diag = None
    while True:
        try:
            lid = _draft_league_id()           # ACTUAL draft league only (never the config fallback)
            if lid:
                rest = await loop.run_in_executor(None, _rest_picks, lid)
                fixed = draft.reconcile(rest)
                if fixed:
                    print(f"[reconcile] healed {fixed} pick(s) from REST authoritative draft detail",
                          flush=True)
                    draft.write()
                # --- roster-diff diagnostic: does mRoster populate DURING the draft? Log only when
                # the numbers change, so the window stays readable. If `rostered` climbs with your
                # picks, live roster-diff recovery is viable; if it stays 0 while picks happen, it's
                # not (rosters, like mDraftDetail, only fill at finalize). Also previews the picks a
                # roster-diff would recover (`missing`). ---
                rosters, raw_entries = await loop.run_in_executor(None, _roster_snapshot, lid)
                rostered = set().union(*rosters.values()) if rosters else set()
                captured = set(draft.drafted)
                missing = rostered - captured
                diag = (len(captured), len(rostered), raw_entries, len(missing))
                if diag != last_diag:
                    last_diag = diag
                    # roster_entries = raw slots (before id extraction). entries=0 all draft -> rosters
                    # aren't live (roster-diff not viable). entries>0 but rostered=0 -> a parse miss.
                    print(f"[roster-diag] captured={diag[0]} rostered={diag[1]} "
                          f"roster_entries={diag[2]} missing_from_capture={diag[3]} "
                          f"extra={len(captured - rostered)}", flush=True)
                    for pid in list(missing)[:5]:
                        tid = next((t for t, s in rosters.items() if pid in s), None)
                        print(f"[roster-diag]   would recover: {draft._resolve(pid)[0]} "
                              f"(id {pid}) on team {tid}", flush=True)
        except Exception as e:  # noqa: BLE001 -- reconciliation must never bring down the scraper
            print(f"[reconcile err] {str(e)[:100]}", flush=True)
        await asyncio.sleep(RECONCILE_SECONDS)


async def _run() -> None:
    from filelock import FileLock, Timeout
    lock = FileLock(config.SCRAPER_LOCK)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        print("Another scraper holds the lock; exiting.")
        return
    try:
        draft = Draft()
        _try_lock_order(draft)   # best effort now; _discover keeps retrying until the draft room opens
        # RECOVER BY DEFAULT. A fresh espn_live process would otherwise draft.write() an EMPTY board
        # on startup and ZERO draft_state.json -- which is exactly what wiped captured picks during
        # the live draft every time the scraper was restarted mid-draft. So we now rebuild from the
        # authoritative, single-draft state file first (then the ws capture log as a fallback) UNLESS
        # a launcher explicitly declares a fresh draft via CB_FRESH_DRAFT=1. The fresh-start launchers
        # (run-draft.bat / launch.py) delete draft_state.json + ws_capture.jsonl before starting, so
        # recovery there is a safe no-op; only a mid-draft restart actually recovers picks.
        # (Legacy CB_RECOVER_FROM_STATE / CB_RECOVER_FROM_CAPTURE are now redundant -- recovery is the
        # default -- but setting either still forces recovery, so old commands keep working.)
        recovered = 0
        if os.environ.get("CB_FRESH_DRAFT") == "1":
            print("CB_FRESH_DRAFT=1 -> starting a fresh board (no recovery).", flush=True)
        else:
            recovered = draft.seed_from_state_file()   # authoritative, bounded to one draft
            src = "draft_state.json"
            if recovered == 0:
                recovered = draft.seed_from_log()      # fall back to the websocket capture log
                src = "ws_capture.jsonl"
            if recovered:
                print(f"recovered {recovered} pick(s) from {src}", flush=True)
        draft.write()
        print("Live ESPN scraper up. Discovering draft targets…", flush=True)
        # Discovery (websocket capture) + reconciliation (REST safety net) run concurrently; both
        # loop forever with their own error guards, so gather never returns until the process ends.
        await asyncio.gather(_discover(draft), _reconcile_loop(draft))
    finally:
        lock.release()


def _replay(path: str) -> None:
    """Offline: feed a captured ws log through the parser and print the reconstructed board -- lets us
    verify exact numbering against real frames without touching a live draft."""
    d = Draft(log=False)
    o = _rest_order()
    if o:
        d.set_order(o)
    for ln in open(path, encoding="utf-8"):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("ev") == "recv":
            d.handle(str(rec.get("data", "")))
    st = d.state()
    print(f"slot_order: {d.slot_order}")
    print(f"total picks: {st['total_picks']}")
    bad = 0
    for r in st["picks"]:
        exp = _snake(d.slot_order.index(r['team_id']) + 1 if r['team_id'] in d.slot_order else 0,
                     r['round'], d.n)
        flag = "" if exp == r["pick"] else "  <-- MISMATCH"
        if flag:
            bad += 1
        print(f"  #{r['pick']:>3} R{r['round']} team {r['team_id']:>2}  {r['player']} {r['pos']}{flag}")
    print(f"\ncontiguous 1..N? {sorted(d.by_overall) == list(range(1, st['total_picks'] + 1))} | "
          f"snake-consistent mismatches: {bad}")


if __name__ == "__main__":
    if "--replay" in sys.argv:
        _replay(sys.argv[sys.argv.index("--replay") + 1])
    else:
        asyncio.run(_run())
