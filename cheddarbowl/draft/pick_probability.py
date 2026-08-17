"""Probability each available player is drafted before my next pick.

This is the module that powers the core question: *can I wait on this player?* It blends
two views over the window of opponent picks until my next selection:

  1. **ADP view** — the market's expectation of when a player goes (Normal around ADP).
  2. **Demand view** — how many of each position the league still needs, plus live run
     momentum, turned into an expected count of picks per position, then a Poisson
     probability that a given player's position gets drafted deep enough to reach him.

A player whose position most teams have already filled gets a low probability (safe to
wait); a scarce, in-demand position gets a high one (grab now).
"""

from __future__ import annotations

import math

import pandas as pd

from cheddarbowl import config
from cheddarbowl.draft.draft_context import DraftContext

# blend weight between the demand view and the ADP view (0..1, toward demand)
DEMAND_WEIGHT = 0.5
DEFAULT_ADP_SD = 12.0     # fallback ADP uncertainty when adp_sd is missing
MOMENTUM_PICKS = 6        # look-back window for run detection
MOMENTUM_STRENGTH = 0.5   # how much a hot run inflates a position's expected picks


def _poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam). k is a positive integer (positional rank)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    # cdf(k-1) = sum_{i=0}^{k-1} e^-lam lam^i / i!  ; sf = 1 - cdf
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def _position_lambdas(avail: pd.DataFrame, ctx: DraftContext) -> dict[str, float]:
    """Expected number of picks spent on each position during the window."""
    window = ctx.window
    if window <= 0:
        return {pos: 0.0 for pos in config.POSITIONS}
    current = ctx.current_pick()
    upper = current + window

    # --- ADP view: how many available players at each pos are "due" in the window ---
    # One masked value_counts instead of a per-position DataFrame slice: slicing avail[pos==p]
    # 6x (plus 6x below) copied a wide frame each time and dominated live-board + forecast cost.
    win = (avail["adp"] >= current) & (avail["adp"] <= upper)
    win_counts = avail.loc[win, "pos"].value_counts()
    adp_implied = {pos: float(win_counts.get(pos, 0)) for pos in config.POSITIONS}

    # --- Demand view: split the window's picks by positional pressure ---
    present = set(avail["pos"].unique())      # positions with any available value (was 6 slices)
    demand = dict(ctx.league_demand)
    # FLEX pressure is added to the FLEX-eligible positions.
    pressure = {}
    for pos in config.POSITIONS:
        base = demand.get(pos, 0.0)
        if pos in config.FLEX_ELIGIBLE:
            base += demand.get("FLEX", 0.0) / len(config.FLEX_ELIGIBLE)
        # pressure only counts if value is actually available at the position
        pressure[pos] = base if pos in present else 0.0
    total_pressure = sum(pressure.values())
    demand_implied = {
        pos: (window * pressure[pos] / total_pressure) if total_pressure else 0.0
        for pos in config.POSITIONS
    }

    # --- blend, then apply run momentum ---
    recent = ctx.recent_positions[-MOMENTUM_PICKS:]
    lambdas = {}
    for pos in config.POSITIONS:
        lam = DEMAND_WEIGHT * demand_implied[pos] + (1 - DEMAND_WEIGHT) * adp_implied[pos]
        if recent:
            share = recent.count(pos) / len(recent)
            baseline_share = 1.0 / len(config.POSITIONS)
            lam *= 1.0 + MOMENTUM_STRENGTH * (share - baseline_share)
        lambdas[pos] = max(0.0, lam)
    return lambdas


def add_pick_probability(avail: pd.DataFrame, ctx: DraftContext) -> pd.DataFrame:
    """Add ``p_taken`` (prob. drafted before my next pick) and ``p_survive`` = 1 - p_taken.

    Also adds ``pos_avail_rank`` (a player's value rank among available at his position),
    which the VONA module reuses.
    """
    avail = avail.copy()
    if avail.empty:
        avail["pos_avail_rank"] = []
        avail["p_taken"] = []
        avail["p_survive"] = []
        return avail

    # value rank within available players at each position (1 = best still on the board)
    avail["pos_avail_rank"] = (
        avail.groupby("pos")["vob"].rank(ascending=False, method="first").astype(int)
    )

    lambdas = _position_lambdas(avail, ctx)
    current = ctx.current_pick()
    my_next = ctx.my_next_pick or (current + ctx.window)

    # Zip over numpy columns instead of DataFrame.iterrows(): iterrows builds a Series per row
    # (~0.5ms each), which dominated forecast + live-board rescoring; this is the same math, ~20x
    # faster. (No numpy erf available, so the per-row normal CDF stays scalar math.erf.)
    ranks = avail["pos_avail_rank"].to_numpy()
    positions = avail["pos"].to_numpy()
    adps = avail["adp"].to_numpy()
    sds = (avail["adp_sd"].to_numpy() if "adp_sd" in avail.columns else [None] * len(avail))
    p_taken = []
    for k, pos, adp, sd in zip(ranks, positions, adps, sds):
        p_demand = _poisson_sf(int(k), lambdas.get(pos, 0.0))
        if pd.notna(adp):
            sigma = float(sd) if (pd.notna(sd) and sd and sd > 0) else DEFAULT_ADP_SD
            # taken before my next pick == draft position < my_next overall pick
            p_adp = _normal_cdf(my_next - 0.5, float(adp), sigma)
            p = DEMAND_WEIGHT * p_demand + (1 - DEMAND_WEIGHT) * p_adp
        else:
            p = p_demand
        p_taken.append(max(0.0, min(1.0, p)))

    avail["p_taken"] = p_taken
    avail["p_survive"] = [1.0 - p for p in p_taken]
    return avail
