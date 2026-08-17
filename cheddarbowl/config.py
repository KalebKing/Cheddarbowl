"""Central configuration for the fantasy draft assistant.

All league-specific settings live here so the rest of the code stays generic.
Edit DRAFT_SLOT on draft day if your position shifts (e.g. 3 -> 4).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# League identity
# ---------------------------------------------------------------------------
LEAGUE_ID = "754228479"   # ESPN league id (was NFL 3370050 pre-migration); used for the header subtitle
SEASON = 2026
NUM_TEAMS = 12

# Your seat in the draft order (1-based). Snake draft. May shift to 4.
DRAFT_SLOT = 3

# Your NFL.com team id (from the owners page; team 5 = "Brooklyn Blisties" / Kaleb).
# Used to detect "your pick" and highlight your roster.
MY_TEAM_ID = 5

# Optional: hard-code the draft order (team ids by snake slot 1..N) once it's posted.
# Leave as None to auto-learn it from round 1 as picks come in.
DRAFT_ORDER: "list[int] | None" = None

# ---------------------------------------------------------------------------
# Scoring (half-PPR)
# ---------------------------------------------------------------------------
SCORING = "half_ppr"  # one of: ppr, half_ppr, standard
PPR = 0.5             # points per reception implied by SCORING

# ---------------------------------------------------------------------------
# Roster: what each of the 12 teams must fill (15 spots total)
#   starters below + 6 bench = 15 rounds
# ---------------------------------------------------------------------------
STARTERS = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "K": 1,
    "DST": 1,
}
FLEX_SPOTS = 1                 # 1 FLEX per team
FLEX_ELIGIBLE = ("RB", "WR")   # this league: FLEX is RB/WR only (no TE)
BENCH_SPOTS = 6
ROSTER_SIZE = sum(STARTERS.values()) + FLEX_SPOTS + BENCH_SPOTS  # -> 15
NUM_ROUNDS = ROSTER_SIZE

# ---------------------------------------------------------------------------
# VBD starter demand: how many players at each position the whole league
# must start. FLEX demand is added on top and later apportioned across
# RB/WR/TE by their value curve inside strategy/vbd.py.
# ---------------------------------------------------------------------------
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

# ---------------------------------------------------------------------------
# Roster construction targets (max you'd roster at each position). Drives the
# bench-phase recommendation weighting: once a position hits its target, the
# engine stops recommending it. RB/WR are set high so they absorb all bench
# depth; QB/TE allow one backup (2 total); K/DST are hard-capped at 1 (never
# a bench K/DST).
# ---------------------------------------------------------------------------
ROSTER_TARGETS = {"QB": 2, "RB": 8, "WR": 8, "TE": 2, "K": 1, "DST": 1}

# Depth-phase appetite per position (used only after starters/FLEX are filled).
# RB/WR high (want depth); QB/TE low (a backup only if value falls); K/DST none.
DEPTH_WEIGHT = {"QB": 0.2, "TE": 0.2, "RB": 0.5, "WR": 0.5, "K": 0.0, "DST": 0.0}

# K/DST are streamable and always available late, so their (real) VBD point-spread would otherwise
# crowd skill players out of the mid-round recommendations. Defer them: only recommend a K/DST once
# you're within this many picks of the END of YOUR draft. You need 1 of each, so 3 leaves a buffer.
KDST_DEFER_UNTIL_PICKS_LEFT = 3

# Forecast view guardrail: don't project K/DST before this round in best-case roster forecasts.
KDST_FORECAST_MIN_ROUND = 10

# ---------------------------------------------------------------------------
# ADP-spread model: a player's actual draft slot ~ Normal(ADP, sigma(ADP)).
# sigma grows with ADP -> chalky (near-deterministic) at the top, probabilistic
# (players fall) deep. Drives BOTH the noisy simulation and the roster forecast,
# so they stay consistent. The "chaos" multiplier is the Chalky<->Chaotic knob.
# ---------------------------------------------------------------------------
SIGMA_K = 0.22    # slope: sigma ≈ 0.22 * ADP
SIGMA_MIN = 2.0   # floor (round-1 chalk)
SIGMA_MAX = 35.0  # cap (deep board is chaotic but bounded)
CHAOS_LEVELS = {"Chalky": 0.6, "Normal": 1.0, "Chaotic": 1.6}
CHAOS_DEFAULT = "Normal"


def league_starter_demand() -> dict[str, int]:
    """Base starter demand per position across all teams (excludes FLEX)."""
    return {pos: n * NUM_TEAMS for pos, n in STARTERS.items()}


FLEX_DEMAND = FLEX_SPOTS * NUM_TEAMS  # total FLEX slots league-wide (-> 12)

# ---------------------------------------------------------------------------
# NFL.com page URLs (scraped directly -- no API). {round} is 1..NUM_ROUNDS.
# ---------------------------------------------------------------------------
_BASE = f"https://fantasy.nfl.com/league/{LEAGUE_ID}"
URL_OWNERS = f"{_BASE}/owners"
URL_PLAYERS = f"{_BASE}/players"
URL_DRAFT_RESULTS = (
    _BASE + "/draftresults?draftResultsDetail={round}"
    "&draftResultsTab=round&draftResultsType=results"
)
# draftResultsDetail=0 = the whole draft (all rounds) in one server-rendered page. This is the
# PRIMARY live-draft source (explicit pick order + direct team attribution); roster-diff is the
# fallback if it ever returns a login wall / unexpected layout.
URL_DRAFT_RESULTS_ALL = URL_DRAFT_RESULTS.format(round=0)

# ---------------------------------------------------------------------------
# Paths — season-partitioned committed data, plus gitignored runtime + secrets.
#
# Layout (multi-year by design):
#   data/seasons/<year>/reference/    teams, adp, id-crosswalk, headshots   (committed)
#   data/seasons/<year>/projections/  preseason.csv, week01.csv, ...        (committed)
#   data/seasons/<year>/draft/        draft_final.json/.csv                 (committed)
#   data/seasons/<year>/weekly/weekNN/  pre/post-game summaries             (committed)
#   data/seasons/<year>/wrapup/       season_summary                        (committed)
#   runtime/   ephemeral state/logs/locks/profiles   (gitignored, never shipped)
#   secrets/   espn_auth, host_token, cookies         (gitignored, never shipped)
#
# The package lives at <repo>/cheddarbowl, so ROOT is the parent of this file's dir.
# ---------------------------------------------------------------------------
import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # .../cheddarbowl
ROOT = os.path.dirname(_PKG_DIR)                         # repo root (parent of the package)

DATA_DIR = os.path.join(ROOT, "data")
SEASONS_DIR = os.path.join(DATA_DIR, "seasons")
RUNTIME_DIR = os.path.join(ROOT, "runtime")
SECRETS_DIR = os.path.join(ROOT, "secrets")


def season_dir(year: int = SEASON) -> str:
    return os.path.join(SEASONS_DIR, str(year))


def reference_dir(year: int = SEASON) -> str:
    return os.path.join(season_dir(year), "reference")


def projections_dir(year: int = SEASON) -> str:
    return os.path.join(season_dir(year), "projections")


def draft_dir(year: int = SEASON) -> str:
    return os.path.join(season_dir(year), "draft")


def weekly_dir(week: int, year: int = SEASON) -> str:
    """Directory for one in-season week's pre/post-game artifacts (created on demand)."""
    d = os.path.join(season_dir(year), "weekly", f"week{int(week):02d}")
    os.makedirs(d, exist_ok=True)
    return d


def wrapup_dir(year: int = SEASON) -> str:
    return os.path.join(season_dir(year), "wrapup")


# --- current-season reference / projection inputs the engine reads ---
PROJECTIONS_CSV = os.path.join(projections_dir(), "preseason.csv")
ADP_CSV = os.path.join(reference_dir(), "adp.csv")
CROSSWALK_CSV = os.path.join(reference_dir(), "player_crosswalk.csv")
# Hand-authored fixes for projections the auto-matcher misses (proj_name -> ESPN player_id).
CROSSWALK_MANUAL_CSV = os.path.join(reference_dir(), "crosswalk_manual.csv")
HEADSHOTS_CSV = os.path.join(reference_dir(), "headshots.csv")
TEAMS_CSV = os.path.join(reference_dir(), "teams.csv")
NFL_PLAYERS_CSV = os.path.join(reference_dir(), "nfl_players.csv")

# --- ephemeral runtime state (regenerated each run; gitignored; never shipped) ---
DRAFT_STATE_JSON = os.path.join(RUNTIME_DIR, "draft_state.json")
SCRAPER_STATUS_JSON = os.path.join(RUNTIME_DIR, "scraper_status.json")  # heartbeat for the UI
SCRAPER_LOCK = os.path.join(RUNTIME_DIR, "scraper.lock")               # filelock -> only 1 poller
SCRAPER_LOG = os.path.join(RUNTIME_DIR, "scraper.log")                 # spawned poller's stdout/err
MANUAL_DRAFT_JSON = os.path.join(RUNTIME_DIR, "manual_draft.json")     # manual-mode fallback picks
PICK_OVERRIDES_JSON = os.path.join(RUNTIME_DIR, "pick_overrides.json") # in-live pick corrections
WS_CAPTURE_JSONL = os.path.join(RUNTIME_DIR, "ws_capture.jsonl")       # websocket capture (crash recovery)
LAUNCH_LOG = os.path.join(RUNTIME_DIR, "launch.log")
BROWSER_PROFILE_DIR = os.path.join(RUNTIME_DIR, "browser_profile")    # persistent ESPN login profile
# Per-session league override (e.g. a mock). If present its keys win; delete to snap back. Written by
# autoconfig each launch, so it's runtime, not committed.
ACTIVE_LEAGUE_JSON = os.path.join(RUNTIME_DIR, "active_league.json")

# --- secrets / per-user credentials (gitignored; MUST never ship) ---
# host_token: LAN mode grants full (host) controls only to the window whose URL carries ?host=<token>;
#   absent file (dev/single-user) => every session is host.  espn_auth: {league_id, year, swid, espn_s2}.
ESPN_AUTH_JSON = os.path.join(SECRETS_DIR, "espn_auth.json")
HOST_TOKEN_FILE = os.path.join(SECRETS_DIR, "host_token.txt")
COOKIES_JSON = os.path.join(SECRETS_DIR, "nfl_cookies.json")  # minted by browser login (legacy)


def active_override() -> dict:
    """{} unless runtime/active_league.json exists. May carry: league_id, my_team_id, name."""
    import json
    try:
        with open(ACTIVE_LEAGUE_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_ov = active_override()
if _ov.get("league_id"):
    LEAGUE_ID = str(_ov["league_id"])
if _ov.get("my_team_id"):
    MY_TEAM_ID = int(_ov["my_team_id"])

# Pre-draft "stale roster" guard: some sources show last season's FULL rosters until the new
# draft starts. If at least this many teams are already full, treat it as carryover (blank
# board) instead of ~180 phantom "picks". A live draft never has this many full teams until
# it's essentially over, so it won't false-trip mid-draft.
STALE_ROSTER_MIN_FULL_TEAMS = NUM_TEAMS - 1

SCRAPER_STALE_SECONDS = 12   # no scraper heartbeat within this many seconds -> "down"
POLL_SECONDS = 3             # how often the scraper re-checks the draft board

# R (for the ffanalytics projections pipeline). R 4.6.1 is installed but not on PATH.
RSCRIPT = r"C:\Program Files\R\R-4.6.1\bin\Rscript.exe"
# Writable per-project R library (system lib needs admin). Passed as R_LIBS_USER.
RLIB = os.path.join(ROOT, ".Rlib")

# Ensure the writable trees exist (idempotent). Reference/projection/draft dirs so a first-run
# build write doesn't fail; runtime + secrets so live writes land even on a fresh clone.
for _d in (RUNTIME_DIR, os.path.join(RUNTIME_DIR, "logs"), SECRETS_DIR,
           reference_dir(), projections_dir(), draft_dir()):
    os.makedirs(_d, exist_ok=True)


def run_rscript(script_path: str, *args: str):
    """Run an R script with the project library on R_LIBS_USER. Returns CompletedProcess."""
    import subprocess

    env = dict(os.environ, R_LIBS_USER=RLIB)
    return subprocess.run(
        [RSCRIPT, script_path, *args], env=env, capture_output=True, text=True
    )
