# Fantasy Draft Assistant — NFL.com league 3370050

Local draft-day assistant: watches the live NFL.com draft, values players with
[ffanalytics](https://github.com/FantasyFootballAnalytics/ffanalytics) projections, and
recommends who to draft using a dynamic strategy (VBD → tiers → pick-probability → VONA).

Snake draft · 12 teams · half-PPR · roster 1QB/2RB/2WR/1TE/1FLEX/1K/1DST + 6 bench.
Your team id and draft slot live in `config.py` (`MY_TEAM_ID`, `DRAFT_SLOT`).

## Components

| Part | Location | What it does |
|------|----------|--------------|
| Live scraper | `scraper/` | Polls the 12 team roster pages, diffs them, reconstructs the draft into `data/draft_state.json`. Pure HTTP, no browser. |
| Projections | `scripts/run_projections.R` | ffanalytics scrape → half-PPR points, ADP, floor/ceiling → `data/projections.csv`. |
| Crosswalk | `scripts/fetch_nfl_players.py`, `scripts/build_crosswalk.py` | Maps ffanalytics players to NFL.com `playerId` → `data/player_crosswalk.csv`. |
| Strategy | `strategy/` | (in progress) VBD, tiers, pick-probability, VONA, recommendations. |
| Dashboard | `app.py` | (planned) Streamlit live board. |

## Setup

Python deps (already in `.venv`):

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

R + ffanalytics (R 4.6.1 installed at `C:\Program Files\R\R-4.6.1`, not on PATH). The
package lives in a per-project library `.Rlib` (system lib needs admin):

```bash
R_LIBS_USER=".Rlib" "C:/Program Files/R/R-4.6.1/bin/Rscript.exe" scripts/install_ffanalytics.R
```

## Pre-draft pipeline (run a day or two before the draft)

```bash
# 1. Projections (slow: scrapes several sites with polite delays)
R_LIBS_USER=".Rlib" "C:/Program Files/R/R-4.6.1/bin/Rscript.exe" scripts/run_projections.R 2026 0

# 2. NFL.com player universe + id crosswalk
.venv/Scripts/python.exe scripts/fetch_nfl_players.py
.venv/Scripts/python.exe scripts/build_crosswalk.py

# 3. Player headshots (pre-warms data/headshots.csv so the dashboard shows images
#    instantly, with nothing fetching during the draft)
.venv/Scripts/python.exe scripts/fetch_headshots.py

# 4. League teams (names, managers, avatars) -> data/teams.csv
.venv/Scripts/python.exe scripts/fetch_teams.py
```

Rebuild the crosswalk close to draft day — early-August NFL.com player pools omit some
rookies, which lowers the match rate until they're added.

## During the draft

Two processes. In one terminal, run the scraper so the draft board stays current:

```bash
.venv/Scripts/python.exe -m scraper.nfl_scraper --poll     # writes data/draft_state.json
```

In another, launch the dashboard (opens in your browser, auto-refreshes every 3s):

```bash
.venv/Scripts/python.exe -m streamlit run app.py
```

The dashboard shows: a "YOUR PICK" banner, ranked recommendations with plain-English
reasons, the best-available board by position (tiers + probability a player is still there
at your next pick), a positional-scarcity strip, and the live draft log.

**Practice / testing:** flip **Data source → Simulated** in the sidebar to drive the whole
dashboard off a fabricated draft (pick-count slider + optional "QB run") — no live draft
needed.

## Tests

```bash
.venv/Scripts/python.exe tests/test_draft_tracker.py
```

## Notes / known gaps

- **DST** is scraped separately from known-good sources (combined aggregation errors in
  ffanalytics for team defenses).
- **FanDuel** source errors on scrape and is excluded — harmless.
- Everything on NFL.com currently loads unauthenticated; if an in-progress draft turns out
  members-only, mint cookies via a one-time browser login into `data/nfl_cookies.json`.
