"""Live draft capture for NFL.com by polling & diffing team roster pages.

Strategy: every ~POLL_SECONDS, fetch all 12 team roster pages (server-rendered HTML) and
diff each roster's set of playerIds against the previous snapshot. A player that newly
appears on a roster is a draft pick. Pick order/round are reconstructed from counts + the
snake order. No browser, no JS, never touches the draft window.

CLI:
    python -m cheddarbowl.ingest.legacy.nfl_scraper --once     # one snapshot of owners + all rosters
    python -m cheddarbowl.ingest.legacy.nfl_scraper --poll      # live loop -> data/draft_state.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field

import requests

# Allow running as a script (python scraper/nfl_scraper.py) or module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl import config
from cheddarbowl.ingest.legacy.parsing import (
    DraftResultPick,
    OwnerInfo,
    PlayerRef,
    parse_draft_results,
    parse_owners,
    parse_roster,
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
def make_session(cookies_path: str | None = None) -> requests.Session:
    """Build a requests session. Optionally load cookies minted by a browser login
    (see scraper/auth.py) for when an in-progress draft is members-only."""
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    if cookies_path and os.path.exists(cookies_path):
        for c in json.load(open(cookies_path, encoding="utf-8")):
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def get_owners(session: requests.Session) -> list[OwnerInfo]:
    return parse_owners(fetch(session, config.URL_OWNERS))


def get_roster(session: requests.Session, team_id: int) -> list[PlayerRef]:
    """All players currently on a team's roster (bench/IR included)."""
    url = f"https://fantasy.nfl.com/league/{config.LEAGUE_ID}/team/{team_id}"
    return [e.player for e in parse_roster(fetch(session, url))]


def snapshot_rosters(session: requests.Session, num_teams: int = config.NUM_TEAMS) -> dict[int, list[PlayerRef]]:
    """{team_id: [PlayerRef, ...]} across all teams for one poll cycle."""
    return {t: get_roster(session, t) for t in range(1, num_teams + 1)}


def get_draft_results(session: requests.Session) -> "list[DraftResultPick] | None":
    """Ordered picks from the server-rendered draft-results page (PRIMARY source).

    None => not the results page (login wall / layout change) => caller uses the roster fallback.
    """
    return parse_draft_results(fetch(session, config.URL_DRAFT_RESULTS_ALL))


# ---------------------------------------------------------------------------
# Draft reconstruction
# ---------------------------------------------------------------------------
@dataclass
class Pick:
    pick: int          # overall pick number (1-based)
    round: int
    team_id: int
    player_id: str
    player: str
    pos: str
    nfl_team: str


@dataclass
class DraftTracker:
    """Accumulates picks from successive roster snapshots and reconstructs order."""

    num_teams: int = config.NUM_TEAMS
    picks: list[Pick] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)  # player_ids already recorded
    draft_order: list[int] = field(default_factory=list)  # team_ids by snake slot 1..N
    baseline_seeded: bool = False  # pre-draft carryover rosters absorbed as baseline
    stale: bool = False            # current snapshot looks like last-season carryover

    def __post_init__(self) -> None:
        if not self.draft_order and getattr(config, "DRAFT_ORDER", None):
            self.draft_order = list(config.DRAFT_ORDER)

    # -- pre-draft staleness ------------------------------------------------
    def _looks_stale(self, snapshot: dict[int, list["PlayerRef"]]) -> bool:
        """True if (nearly) every team is already at a full roster -> last-season carryover.

        A live snake draft fills teams evenly from empty, so it never has this many full
        rosters until it's essentially complete; a full board *before* the draft is stale.
        """
        full = sum(1 for players in snapshot.values() if len(players) >= config.ROSTER_SIZE)
        return full >= config.STALE_ROSTER_MIN_FULL_TEAMS

    @staticmethod
    def _total(snapshot: dict[int, list["PlayerRef"]]) -> int:
        return sum(len(v) for v in snapshot.values())

    # -- snake helpers ------------------------------------------------------
    def _round_of(self, overall_pick: int) -> int:
        return math.ceil(overall_pick / self.num_teams)

    def expected_slot(self, overall_pick: int) -> int:
        """1-based draft slot on the clock for a given overall pick (snake)."""
        idx = (overall_pick - 1) % self.num_teams
        rnd = self._round_of(overall_pick)
        return idx + 1 if rnd % 2 == 1 else self.num_teams - idx

    def team_on_clock(self, overall_pick: int) -> int | None:
        slot = self.expected_slot(overall_pick)
        if 1 <= slot <= len(self.draft_order):
            return self.draft_order[slot - 1]
        return None

    def _slot_rank(self, team_id: int) -> int:
        """Sort rank for a team by its draft slot (unknown teams sort last)."""
        return self.draft_order.index(team_id) if team_id in self.draft_order else self.num_teams + team_id

    def _order_fresh(self, fresh: list[tuple[int, "PlayerRef"]]) -> list[tuple[int, "PlayerRef"]]:
        """Sequence this cycle's fresh picks to match the snake order.

        Greedily assign each of the next `len(fresh)` overall picks to a fresh pick from
        the team the snake expects on the clock; if none matches (order still unknown or
        the board desynced), fall back to the lowest-slot remaining pick.
        """
        remaining = list(fresh)
        ordered: list[tuple[int, PlayerRef]] = []
        base = len(self.picks)
        for i in range(len(fresh)):
            expected = self.team_on_clock(base + i + 1)
            idx = next((j for j, (tid, _) in enumerate(remaining) if tid == expected), None)
            if idx is None:
                idx = min(range(len(remaining)), key=lambda j: self._slot_rank(remaining[j][0]))
            ordered.append(remaining.pop(idx))
        return ordered

    # -- ingest -------------------------------------------------------------
    def update(self, snapshot: dict[int, list[PlayerRef]]) -> list[Pick]:
        """Diff a fresh snapshot against what we've seen; record & return new picks.

        New players found in one cycle are ordered by the snake sequence so multiple
        picks landing between polls get sensible overall-pick numbers.
        """
        # Pre-draft carryover: NFL.com shows last season's full rosters until the draft
        # starts. On our first sight of a full board, absorb everyone as a baseline and emit
        # NO picks, so the dashboard stays blank instead of showing ~180 phantom picks.
        if not self.picks and not self.baseline_seeded and self._looks_stale(snapshot):
            for players in snapshot.values():
                for p in players:
                    self._seen.add(p.player_id)
            self.baseline_seeded = True
            self.stale = True
            return []
        # Draft day: those carryover rosters get wiped to (near) empty. Detect that reset and
        # start fresh so the real picks are counted from pick #1.
        if self.baseline_seeded and self._total(snapshot) < self.num_teams:
            self._seen.clear()
            self.picks.clear()
            self.baseline_seeded = False
            self.stale = False
        else:
            self.stale = self.baseline_seeded  # still holding a baselined carryover board

        # Collect brand-new (team_id, player) pairs this cycle.
        fresh: list[tuple[int, PlayerRef]] = []
        for team_id, players in snapshot.items():
            for p in players:
                if p.player_id not in self._seen:
                    fresh.append((team_id, p))

        # Learn draft order from round 1 as teams first appear (unless preconfigured).
        if len(self.picks) < self.num_teams and not getattr(config, "DRAFT_ORDER", None):
            for team_id, _ in fresh:
                if team_id not in self.draft_order:
                    self.draft_order.append(team_id)

        # Order this cycle's fresh picks by the snake sequence (handles round reversal
        # and back-to-back turn picks). Falls back to slot order if order is unknown.
        ordered = self._order_fresh(fresh)

        new_picks: list[Pick] = []
        for team_id, p in ordered:
            self._seen.add(p.player_id)
            overall = len(self.picks) + 1
            pk = Pick(
                pick=overall,
                round=self._round_of(overall),
                team_id=team_id,
                player_id=p.player_id,
                player=p.name,
                pos=p.pos,
                nfl_team=p.nfl_team,
            )
            self.picks.append(pk)
            new_picks.append(pk)
        return new_picks

    # -- output -------------------------------------------------------------
    def drafted_ids(self) -> set[str]:
        return set(self._seen)

    def to_json(self) -> dict:
        next_overall = len(self.picks) + 1
        return {
            "num_teams": self.num_teams,
            "total_picks": len(self.picks),
            "draft_order": self.draft_order,
            "on_the_clock": {
                "overall_pick": next_overall,
                "round": self._round_of(next_overall),
                "team_id": self.team_on_clock(next_overall),
            },
            "picks": [asdict(p) for p in self.picks],
        }

    def write(self, path: str = config.DRAFT_STATE_JSON) -> None:
        write_draft_state(self.to_json(), path)


def write_draft_state(state: dict, path: str = config.DRAFT_STATE_JSON) -> None:
    """Atomically write a draft_state dict so the UI never reads a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


@dataclass
class DraftResultsTracker:
    """Turns the server-rendered draft-results page (a full ordered snapshot) into draft_state.

    Simpler than the roster tracker: the page already gives explicit pick order + team, so no
    diffing. It only needs the same pre-draft staleness guard -- before the draft, this page
    shows LAST season's completed 180-pick draft, which we blank until the real draft begins.
    """

    num_teams: int = config.NUM_TEAMS
    seen_live: bool = False   # have we ever seen a partial (in-progress) board this session?
    stale: bool = False       # current snapshot looks like last-season carryover
    draft_order: list[int] = field(default_factory=list)

    def _round_of(self, overall_pick: int) -> int:
        return math.ceil(overall_pick / self.num_teams)

    def _team_on_clock(self, overall_pick: int) -> int | None:
        idx = (overall_pick - 1) % self.num_teams
        slot = idx + 1 if self._round_of(overall_pick) % 2 == 1 else self.num_teams - idx
        return self.draft_order[slot - 1] if 1 <= slot <= len(self.draft_order) else None

    def build(self, picks: list[DraftResultPick]) -> dict:
        full = self.num_teams * config.NUM_ROUNDS
        # Carryover guard: a COMPLETE draft seen before we've ever observed a live/partial one
        # is last season's results -> hold the board blank (mirrors the roster stale logic).
        if not self.seen_live and len(picks) >= full:
            self.stale = True
            self.draft_order = []
            return self._state([])
        if len(picks) < full:
            self.seen_live = True   # a partial board means this year's draft has begun
        self.stale = False
        # Round 1 is drafted in slot order, so the first N picks reveal the snake order.
        self.draft_order = [p.team_id for p in picks[: self.num_teams]]
        return self._state(picks)

    def _state(self, picks: list[DraftResultPick]) -> dict:
        rows = [{
            "pick": p.pick, "round": self._round_of(p.pick), "team_id": p.team_id,
            "player_id": p.player.player_id, "player": p.player.name,
            "pos": p.player.pos, "nfl_team": p.player.nfl_team,
        } for p in picks]
        nxt = len(rows) + 1
        return {
            "num_teams": self.num_teams, "total_picks": len(rows),
            "draft_order": list(self.draft_order),
            "on_the_clock": {"overall_pick": nxt, "round": self._round_of(nxt),
                             "team_id": self._team_on_clock(nxt)},
            "picks": rows,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_once(session: requests.Session) -> None:
    owners = get_owners(session)
    print(f"Owners ({len(owners)} teams):")
    for o in owners:
        print(f"  team {o.team_id:>2}: {o.team_name}  -  {o.manager}")

    print("\nPRIMARY source — draft results page:")
    dr_picks = get_draft_results(session)
    if dr_picks is None:
        print("  NOT available (login wall / unexpected layout) — the poller would fall back "
              "to roster-diffing.")
    else:
        print(f"  {len(dr_picks)} picks parsed. First few:")
        for p in dr_picks[:5]:
            r = p.player
            print(f"    #{p.pick:>3} -> team {p.team_id:>2}  {r.name} {r.pos}-{r.nfl_team}")

    print("\nFALLBACK source — team rosters:")
    snap = snapshot_rosters(session)
    for t, players in snap.items():
        print(f"  team {t:>2}: {len(players):>2} players  e.g. {[p.name for p in players[:2]]}")
    tracker = DraftTracker()
    new = tracker.update(snap)
    print(f"  Derived {len(new)} picks from current rosters "
          f"(learned order: {tracker.draft_order}).")


def _write_status(ok: bool, picks: int, error: str | None = None, stale: bool = False,
                  source: str = "") -> None:
    """Heartbeat the UI reads to show scraper health / trip the failsafe."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = config.SCRAPER_STATUS_JSON + ".tmp"
    payload = {"pid": os.getpid(), "ts": time.time(), "picks": picks,
               "ok": ok, "error": error, "stale": stale, "source": source}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, config.SCRAPER_STATUS_JSON)


def _poll_once(session: requests.Session, dr: "DraftResultsTracker",
               rosters: "DraftTracker") -> tuple[dict, str, bool]:
    """One poll: try the draft-results page first; fall back to roster-diffing.

    Returns (draft_state, source, stale). ``source`` is "draftresults" or "rosters".
    """
    try:
        picks = get_draft_results(session)   # None => login wall / not the results page
    except Exception as e:  # noqa: BLE001 - network hiccup -> try the fallback this cycle
        print(f"\n[warn] draftresults fetch failed ({e}); using roster fallback", end="")
        picks = None
    if picks is not None:
        return dr.build(picks), "draftresults", dr.stale
    rosters.update(snapshot_rosters(session))
    return rosters.to_json(), "rosters", rosters.stale


def _cmd_poll(session: requests.Session) -> None:
    # Singleton guard: hold an exclusive OS lock for the life of the poller. A second poller
    # (e.g. spawned by another browser tab) fails to acquire it and exits immediately, so at
    # most one scraper ever runs regardless of how many times the app tries to start one.
    from filelock import FileLock, Timeout

    lock = FileLock(config.SCRAPER_LOCK)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        print("Another scraper already holds the lock; exiting.")
        return

    dr, rosters = DraftResultsTracker(), DraftTracker()
    last_n, last_src = -1, ""
    print(f"Polling draft results every {config.POLL_SECONDS}s (roster-diff fallback). "
          f"Ctrl-C to stop.")
    try:
        while True:
            try:
                state, source, stale = _poll_once(session, dr, rosters)
                write_draft_state(state)
                n = state["total_picks"]
                _write_status(ok=True, picks=n, stale=stale, source=source)
                if source != last_src:
                    print(f"\n[source: {source}]")
                if stale:
                    print("  … draft not started (last-season results) — board blank", end="\r")
                elif n != last_n:
                    print(f"  … {n} picks captured via {source}", end="\r")
                last_n, last_src = n, source
                time.sleep(config.POLL_SECONDS)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:  # noqa: BLE001 - keep the draft loop alive
                print(f"\n[warn] poll error: {e}; retrying in {config.POLL_SECONDS}s")
                _write_status(ok=False, picks=max(0, last_n), error=str(e), source=last_src)
                time.sleep(config.POLL_SECONDS)
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="NFL.com roster-diff draft scraper")
    ap.add_argument("--once", action="store_true", help="one snapshot of owners + rosters")
    ap.add_argument("--poll", action="store_true", help="live poll loop -> draft_state.json")
    ap.add_argument("--cookies", default=config.COOKIES_JSON, help="path to cookies json")
    args = ap.parse_args(argv)

    session = make_session(args.cookies)
    if args.poll:
        _cmd_poll(session)
    else:
        _cmd_once(session)


if __name__ == "__main__":
    main()
