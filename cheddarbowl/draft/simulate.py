"""Dev tool: simulate a draft by ADP to a given pick, then show the engine's advice.

Not part of the live app — it fabricates a draft_state so we can eyeball recommendations
(and the wait/grab logic) on real projection data without waiting for a real draft.

    python scripts/simulate_draft.py [num_picks] [--qb-run]
"""

from __future__ import annotations

import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from cheddarbowl import config
from cheddarbowl.data_loader import load_players
from cheddarbowl.draft.draft_context import snake_pick_numbers
from cheddarbowl.draft.engine import Engine, _fmt_recs

# Draft order that seats MY_TEAM_ID at DRAFT_SLOT so the demo lines up with config.
# Compute both indices up front, then swap — computing .index() inside the assignment
# would find the team at its already-moved position and undo the swap.
_ORDER = list(range(1, config.NUM_TEAMS + 1))
_i, _j = config.DRAFT_SLOT - 1, _ORDER.index(config.MY_TEAM_ID)
_ORDER[_i], _ORDER[_j] = _ORDER[_j], _ORDER[_i]


@functools.lru_cache(maxsize=1)
def _prepared_players() -> "pd.DataFrame":
    """Player pool with VBD (vob) + ADP -- loaded and VBD-computed ONCE, then reused across
    every sim (a Monte Carlo runs hundreds). simulate() only reads it (assign/filter return
    copies), so sharing the cached frame is safe and turns a ~150ms/call reload into ~0."""
    from cheddarbowl.draft.vbd import compute_vbd
    players, _, _ = compute_vbd(load_players())
    return players


def _mandatory_remaining(counts: dict) -> int:
    """How many required roster slots (all starters + FLEX) a team has yet to fill."""
    need = sum(max(0, n - counts.get(pos, 0)) for pos, n in config.STARTERS.items())
    surplus = sum(max(0, counts.get(p, 0) - config.STARTERS.get(p, 0))
                  for p in config.FLEX_ELIGIBLE)
    need += max(0, config.FLEX_SPOTS - surplus)
    return need


def _team_on_clock(overall: int, order: "list[int] | None" = None) -> int:
    order = order or _ORDER
    idx = (overall - 1) % config.NUM_TEAMS
    rnd = (overall - 1) // config.NUM_TEAMS + 1
    slot = idx + 1 if rnd % 2 == 1 else config.NUM_TEAMS - idx
    return order[slot - 1]


def simulate(num_picks: int, qb_run: bool = False, chaos: float = 1.0,
             seed: int | None = None, locked: "list[dict] | None" = None,
             reroll: int = 0, overrides: "dict[int, str] | None" = None,
             order: "list[int] | None" = None) -> dict:
    # Opponents draft by a *realized* draft slot sampled from Normal(ADP, sigma(ADP)) -- the
    # same ADP-spread model the forecast uses, so the two are consistent. YOUR team drafts
    # like the tool (best need-filling value). Deterministic given `seed`.
    #
    # Lock-past / re-roll-future: `locked` is a committed prefix of pick dicts that is replayed
    # verbatim (never regenerated) -- only picks AFTER it are drawn. `reroll` perturbs the seed
    # for that remainder, so bumping it redraws the rest of the draft while the locked prefix
    # stays put. The app stores the prefix in session_state (see app._simulated_state); a fresh
    # page load with no lock (locked=None, reroll=0) reproduces the canonical draw from `seed`.
    import random

    from cheddarbowl.draft.adp_model import draft_position_sd
    from cheddarbowl.draft.forecast import _need_factor

    overrides = overrides or {}  # {overall_pick: player_id} -- forced picks (your interactive choices)
    order = order or _ORDER      # draft-slot order [slot1_team..slotN_team]; default seats you at DRAFT_SLOT

    players = _prepared_players()
    base_seed = seed if seed is not None else int(round(chaos * 1000)) * 7 + 12345
    # reroll==0 -> canonical draw (so an unlocked/unrolled draft is stable); reroll>0 -> a
    # fresh, still-deterministic draw for the post-lock remainder only.
    rseed = base_seed if reroll <= 0 else (base_seed ^ (int(reroll) * 2654435761 + 1))
    rng = random.Random(rseed)

    def _realized(adp: float) -> float:
        if pd.isna(adp):
            return 9000.0 + rng.random() * 1000  # no ADP -> deep, mild jitter
        return float(adp) + rng.gauss(0.0, draft_position_sd(adp, chaos))

    # Draw realized slots once (fixed order -> stable regardless of num_picks / slider).
    players = players.assign(realized=[_realized(a) for a in players["adp"]])

    taken: set[str] = set()
    picks = []
    counts: dict[int, dict[str, int]] = {}  # per-team position counts

    def _bump(tid: int, pos: str) -> None:
        counts.setdefault(tid, {})[pos] = counts.setdefault(tid, {}).get(pos, 0) + 1

    # 1) Replay the committed prefix verbatim -- these picks are frozen and never re-drawn.
    for p in (locked or [])[:num_picks]:
        taken.add(p["player_id"])
        _bump(p["team_id"], p["pos"])
        picks.append(dict(p))

    # 2) Generate the remainder from the (possibly re-rolled) realized slots.
    for overall in range(len(picks) + 1, num_picks + 1):
        pool = players[~players["player_id"].isin(taken)]
        if pool.empty:
            break
        tid = _team_on_clock(overall, order)
        tcounts = counts.get(tid, {})
        forced = overrides.get(overall)
        match = (pool[pool["player_id"].astype(str) == str(forced)]
                 if forced is not None else None)
        if match is not None and not match.empty:
            row = match.iloc[0]                    # user-forced pick (your choice in interactive sim)
        elif tid == config.MY_TEAM_ID:
            # Same two-bucket shape as the opponents, but ranked by VALUE (VBD vob) instead of
            # ADP: fill skill starters + FLEX first, then bench depth -- with K/DST deferred
            # behind bench (their low vob keeps them last) until the endgame forcing guarantees
            # completion. Mirrors the tool's own recommendation logic.
            cand = pool.assign(nf=pool["pos"].map(lambda p: _need_factor(tcounts, p)))
            elig = cand[cand["nf"] > 0]
            if config.NUM_ROUNDS - sum(tcounts.values()) <= _mandatory_remaining(tcounts):
                forced = elig[elig["nf"] >= 1.0]                    # endgame: fill what's left (K/DST)
                elig = forced if not forced.empty else elig
            else:
                # VBD overvalues an elite K/DST vs a deep bench flier, so never spend an early
                # pick on them -- defer entirely to the endgame forcing above. Skill starters
                # first, then bench depth, both by value.
                elig = elig[~elig["pos"].isin(("K", "DST"))]
                starters = elig[elig["nf"] >= 1.0]
                elig = starters if not starters.empty else elig
            row = (elig if not elig.empty else cand).sort_values("vob", ascending=False).iloc[0]
        elif qb_run and overall <= 24 and overall % 2 == 0:
            qbs = pool[pool["pos"] == "QB"].sort_values("realized")
            row = qbs.iloc[0] if len(qbs) else pool.sort_values("realized").iloc[0]
        else:
            # Opponents draft by ADP (realized) inside roster construction, in two buckets:
            #   1) fill unfilled SKILL starters + FLEX first (QB/RB/WR/TE -- never K/DST, which
            #      wait), so early picks land in the lineup, not on the bench;
            #   2) then everything else -- bench depth and K/DST -- by ADP (K/DST have late ADP,
            #      so they naturally fall to the end).
            # Never overload a position past its target (nf>0). Once picks run low, restrict to
            # still-unfilled mandatory starters (nf>=1, K/DST included) so every team finishes
            # legal instead of lopsided/incomplete.
            cand = pool.assign(nf=pool["pos"].map(lambda p: _need_factor(tcounts, p)))
            elig = cand[cand["nf"] > 0]
            if config.NUM_ROUNDS - sum(tcounts.values()) <= _mandatory_remaining(tcounts):
                forced = elig[elig["nf"] >= 1.0]                    # endgame: fill what's left
                elig = forced if not forced.empty else elig
            else:
                starters = elig[(elig["nf"] >= 1.0) & (~elig["pos"].isin(("K", "DST")))]
                elig = starters if not starters.empty else elig     # skill starters before bench
            row = (elig if not elig.empty else cand).sort_values("realized").iloc[0]
        taken.add(row["player_id"])
        _bump(tid, row["pos"])
        rnd = (overall - 1) // config.NUM_TEAMS + 1
        picks.append({
            "pick": overall, "round": rnd, "team_id": tid,
            "player_id": row["player_id"], "player": row["name"],
            "pos": row["pos"], "nfl_team": row["team"],
        })
    return {"num_teams": config.NUM_TEAMS, "total_picks": num_picks,
            "draft_order": order, "on_the_clock": {}, "picks": picks}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    num = int(args[0]) if args else 26
    qb_run = "--qb-run" in sys.argv

    state = simulate(num, qb_run=qb_run)
    eng = Engine()
    ctx, avail, recs = eng.evaluate(state)
    label = "QB-RUN scenario" if qb_run else "ADP scenario"
    print(f"=== {label}: {num} picks made, my pick #{ctx.current_pick()} "
          f"(slot {ctx.slot}), window {ctx.window} ===")
    print(f"League remaining starter demand (excl. me): "
          f"{ {k: int(v) for k, v in ctx.league_demand.items() if v} }")
    # How many of each position are already gone?
    gone = {}
    for p in state["picks"]:
        gone[p["pos"]] = gone.get(p["pos"], 0) + 1
    print(f"Positions drafted so far: {gone}")
    print(f"\nTop recommendations:\n{_fmt_recs(recs)}")

    # Spotlight the best available QB vs RB to show wait/grab divergence.
    best = avail.sort_values("vob", ascending=False)
    for pos in ("QB", "RB", "WR"):
        r = best[best["pos"] == pos].head(1)
        if len(r):
            r = r.iloc[0]
            verdict = "WAIT" if r["p_taken"] < 0.4 else ("GRAB" if r["p_taken"] > 0.7 else "lean")
            print(f"  best {pos}: {r['name']:<20} vob={r['vob']:6.1f} "
                  f"p_gone={r['p_taken']:4.0%} -> {verdict}")


if __name__ == "__main__":
    main()
