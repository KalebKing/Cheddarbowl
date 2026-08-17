"""Roster forecast: a realistic best-case projection of a team's remaining picks.

Walk the team's remaining snake picks in order; at each one, take the best need-filling
available player who has a realistic chance (>= SURVIVE_THRESHOLD) of still being there at
that pick, removing them from the pool as we go. Uses the same survival model as the live
recommendations, projected forward to each future pick.
"""

from __future__ import annotations

import pandas as pd

from cheddarbowl import config
from cheddarbowl.draft.adp_model import survival_prob
from cheddarbowl.draft.draft_context import DraftContext, snake_pick_numbers

SURVIVE_THRESHOLD = 0.30  # "best case": a realistic shot of surviving to the pick
FORECAST_POOL_CAP = 60    # score only the top-N survivors by value per pick (perf; #1 is always here)

_FC_CACHE: dict = {}      # forecast memo (see forecast_team); wholesale-cleared when it grows large
_FC_CACHE_MAX = 48


def _forecast_sig(board: pd.DataFrame, state: dict, team_id: int,
                  threshold: float, chaos: float, pin_first=None) -> "tuple | None":
    """Hashable fingerprint of everything forecast_team's result depends on. The board vob-sum of
    the top rows distinguishes the value lens (Floor/Proj/Ceil give different vob) and the current
    available set, so a lens flip or a new pick misses the cache; a repeat render hits it.
    ``pin_first`` (the what-if player) is part of the key so baseline and what-if cache separately."""
    if board is None or board.empty:
        return None
    picks = tuple((p.get("pick"), str(p.get("player_id"))) for p in state.get("picks", []))
    vob_tag = round(float(board["vob"].head(40).sum()), 2)
    return (picks, int(team_id), round(float(threshold), 3), round(float(chaos), 4), vob_tag,
            str(pin_first) if pin_first is not None else None)


def team_pos_counts(state: dict, team_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in state.get("picks", []):
        if p["team_id"] == team_id:
            counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    return counts


def team_future_picks(state: dict, team_id: int) -> list[int]:
    """Overall pick numbers still ahead of us for this team (snake order)."""
    order = state.get("draft_order") or []
    if team_id not in order:
        return []
    slot = order.index(team_id) + 1
    current = state.get("total_picks", 0) + 1
    return [p for p in snake_pick_numbers(slot) if p >= current]


def _need_factor(counts: dict[str, int], pos: str) -> float:
    """Depth-target roster need for an arbitrary team's counts (mirrors DraftContext)."""
    count = counts.get(pos, 0)
    target = config.ROSTER_TARGETS.get(pos, 0)
    if count >= target:
        return 0.0
    if max(0, config.STARTERS.get(pos, 0) - count) > 0:
        return 1.0
    if pos in config.FLEX_ELIGIBLE:
        surplus = sum(max(0, counts.get(p, 0) - config.STARTERS.get(p, 0))
                      for p in config.FLEX_ELIGIBLE)
        if max(0, config.FLEX_SPOTS - surplus) > 0:
            return 1.0
    starter = config.STARTERS.get(pos, 0)
    depth_total = max(1, target - starter)
    return config.DEPTH_WEIGHT.get(pos, 0.3) * (target - count) / depth_total


def survival_to(pool: pd.DataFrame, ctx: DraftContext, pick: int,
                chaos: float = 1.0) -> pd.DataFrame:
    """Per-player P(still available at `pick`) from the ADP-spread model (chalky<->chaotic).
    Immediate/own pick -> everyone available."""
    if pick <= ctx.current_pick() or pool.empty:
        return pool[["player_id"]].assign(surv=1.0)
    surv = pool["adp"].map(lambda a: survival_prob(a, pick, chaos))
    return pool[["player_id"]].assign(surv=surv.to_numpy())


def forecast_team(board: pd.DataFrame, state: dict, ctx: DraftContext, team_id: int,
                  threshold: float = SURVIVE_THRESHOLD, chaos: float = 1.0,
                  pin_first: "str | None" = None) -> list[dict]:
    """Project a team's remaining picks by *following the recommendations forward*.

    At each of the team's future picks: keep the realistic survivors (>= threshold), then take
    the player the recommender would rank #1 for THAT team at THAT pick — the exact same
    ``need_factor * (vob + p_taken*vona)`` math the live ★ panel uses, via a projected context.
    So the top of a team's forecast equals its live recommendation, and the whole path is 'what
    if I follow my own advice each round.'

    ``pin_first`` forces a specific player at the team's FIRST future pick (the "what-if": *if I
    take X at my next pick*), then follows the recommendations for the rest — so you can see how a
    different pick now reshapes the whole downstream roster. Ignored if that player is already off
    the board.

    The projected context is rebuilt per pick from a synthetic state: the team on the clock at
    ``pick``, with its already-forecast picks consumed so its roster needs advance. Opponents'
    interim picks are unknown, so league demand between the team's picks is mildly optimistic —
    the survival filter absorbs most of it. K/DST deferral falls out of ``need_factor`` (it sinks
    a filled/streamable position), so there's no separate late-round gate. Returns one record per
    future pick.

    Memoized: any Streamlit widget interaction reruns the whole script, so without a cache this
    ~0.5s walk would recompute on every board-tab / view click. The key captures everything the
    result depends on — draft progress, the team, the value lens (via a board vob fingerprint),
    and chaos — so it recomputes only on a genuine change.
    """
    from cheddarbowl.draft.recommend import apply_scores

    sig = _forecast_sig(board, state, team_id, threshold, chaos, pin_first)
    if sig is not None and sig in _FC_CACHE:
        return _FC_CACHE[sig]

    pool = board.copy()
    real_picks = state.get("picks", [])
    order = state.get("draft_order") or []
    consumed: list[dict] = []      # this team's hypothetical picks, appended to the synth state
    out: list[dict] = []

    def _take(best, pick, rnd, surv):
        out.append({
            "player_id": best["player_id"], "name": best["name"], "pos": best["pos"],
            "team": best["team"], "proj_pts": float(best["proj_pts"]),
            "forecast_pick": int(pick), "round": rnd, "p_survive": float(surv),
        })
        consumed.append({"pick": int(pick), "team_id": team_id, "round": rnd,
                         "player_id": best["player_id"], "pos": best["pos"],
                         "player": best["name"], "nfl_team": best.get("team")})

    for i, pick in enumerate(team_future_picks(state, team_id)):
        if pool.empty:
            break
        rnd = (pick - 1) // config.NUM_TEAMS + 1

        # What-if: force the considered player at the first future pick (if still available).
        if i == 0 and pin_first is not None:
            row = pool[pool["player_id"].astype(str) == str(pin_first)]
            if not row.empty:
                best = row.iloc[0]
                _take(best, pick, rnd, 1.0)     # you're taking them -> certain
                pool = pool[pool["player_id"] != best["player_id"]]
                continue
            # already drafted -> fall through and forecast this pick normally

        # Survival per player, as a numpy array aligned to pool rows -> mask directly instead of a
        # full-pool merge (the merge copied the whole board each pick and cost as much as scoring).
        surv = survival_to(pool, ctx, pick, chaos)["surv"].to_numpy()
        surv_by_pid = dict(zip(pool["player_id"].to_numpy(), surv))
        keep = surv >= threshold
        cand = pool[keep] if keep.any() else pool
        cand = cand.nlargest(FORECAST_POOL_CAP, "vob")
        if cand.empty:
            break

        pctx = DraftContext.build(
            {"draft_order": order, "total_picks": pick - 1, "picks": real_picks + consumed},
            team_id=team_id)
        scored = apply_scores(cand, pctx)
        if scored.empty:
            break
        best = scored.iloc[0]     # #1 by the recommender's score (vob is the tiebreak)
        _take(best, pick, rnd, surv_by_pid.get(best["player_id"], 1.0))
        pool = pool[pool["player_id"] != best["player_id"]]

    if sig is not None:
        if len(_FC_CACHE) >= _FC_CACHE_MAX:
            _FC_CACHE.clear()
        _FC_CACHE[sig] = out
    return out
