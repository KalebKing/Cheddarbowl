"""Turn value + scarcity + timing into a ranked "draft now" list with reasons.

Score = need_factor * (VOB + URGENCY_WEIGHT * p_taken * VONA)

  - VOB is the cross-position floor value of the player.
  - p_taken * VONA is the *expected value you lose by waiting*: high only when the player is
    both likely gone AND has a real drop-off behind him. This is what makes the engine grab
    a scarce, about-to-run position over an equal-value player at a safe one.
  - need_factor down-weights positions you've already filled.

Each recommendation carries a plain-English reason assembled from the same signals.
"""

from __future__ import annotations

import pandas as pd

from cheddarbowl import config
from cheddarbowl.draft.draft_context import DraftContext
from cheddarbowl.draft.pick_probability import add_pick_probability
from cheddarbowl.draft.vona import add_vona

URGENCY_WEIGHT = 1.0
RUN_SHARE = 0.5      # >= half of recent picks at one pos == a "run"


def _reason(row: pd.Series, ctx: DraftContext) -> str:
    bits = []
    p = row["p_taken"]
    pos = row["pos"]

    # roster need
    if ctx.my_starter_need.get(pos, 0) > 0:
        bits.append(f"fills your open {pos}")
    elif pos in config.FLEX_ELIGIBLE and ctx.my_flex_need > 0:
        bits.append("fills your FLEX")

    # run detection
    recent = ctx.recent_positions
    if recent and recent.count(pos) / len(recent) >= RUN_SHARE and len(recent) >= 3:
        bits.append(f"{pos} run ({recent.count(pos)} of last {len(recent)})")

    # urgency vs. wait
    if p >= 0.75 and row["vona"] >= 8:
        bits.append(f"likely gone ({p:.0%}); next {pos} drops {row['vona']:.0f} pts - take now")
    elif p <= 0.35:
        nxt = row.get("next_available")
        tail = f" ({nxt} likely back)" if nxt else ""
        bits.append(f"safe to wait ({p:.0%} gone{tail})")
    elif row["vona"] >= 12:
        bits.append(f"tier cliff: {row['vona']:.0f}-pt drop to next {pos}")

    # value vs ADP
    adp = row.get("adp")
    if pd.notna(adp):
        slid = adp - ctx.current_pick()
        if slid >= 10:
            bits.append(f"fell {slid:.0f} past ADP")

    if row.get("last_in_tier"):
        bits.append(f"last in {pos} tier {int(row.get('tier', 0))}")

    return "; ".join(bits) if bits else f"best available {pos} by value"


def apply_scores(available: pd.DataFrame, ctx: DraftContext) -> pd.DataFrame:
    """Numeric scoring only: add p_taken, vona, need_factor, urgency, score; sort by score.

    No plain-English reasons and no rec_rank — that's the slow per-row pass. The forecast walks
    ~15 future picks and only needs the argmax, so it uses this directly (see strategy.forecast);
    ``score_board`` layers the reasons on top for the live recommendation panel.
    """
    avail = add_pick_probability(available, ctx)
    avail = add_vona(avail)
    if avail.empty:
        return avail

    avail["need_factor"] = avail["pos"].map(ctx.need_factor)
    avail["urgency"] = avail["p_taken"] * avail["vona"].clip(lower=0)
    avail["score"] = avail["need_factor"] * (avail["vob"] + URGENCY_WEIGHT * avail["urgency"])
    # A position at its roster target (need_factor 0) must rank BELOW every player you still have a
    # use for -- not at score 0. Otherwise, late in the draft, filled K/DST (score 0) float ABOVE the
    # remaining skill players whose VOB has gone negative (below replacement). Sink them hard. vob is
    # the tiebreak so the sunk (all-equal) group stays deterministically ordered by value.
    avail.loc[avail["need_factor"] <= 0, "score"] = -1e9
    return avail.sort_values(["score", "vob"], ascending=False).reset_index(drop=True)


def score_board(available: pd.DataFrame, ctx: DraftContext) -> pd.DataFrame:
    """Annotate + score the FULL available pool (p_taken, vona, score, reason, rec_rank).

    Returned in recommendation order. The dashboard uses the whole board; callers wanting
    just the shortlist take ``.head(n)`` or use ``recommend``.
    """
    avail = apply_scores(available, ctx)
    if avail.empty:
        return avail
    avail["reason"] = [_reason(r, ctx) for _, r in avail.iterrows()]
    avail["rec_rank"] = range(1, len(avail) + 1)
    return avail


def recommend(available: pd.DataFrame, ctx: DraftContext, top_n: int = 10) -> pd.DataFrame:
    """Top-N shortlist for the current pick. Input must have vob/tier columns."""
    board = score_board(available, ctx)
    return board.head(top_n) if (top_n and not board.empty) else board
