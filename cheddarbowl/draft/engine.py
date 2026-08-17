"""Orchestrates the strategy engine: static value board + live recommendations.

Static board (VBD + tiers) is computed once from projections. Given a draft_state, the
engine marks drafted players, builds live context, and produces ranked recommendations.
"""

from __future__ import annotations

import pandas as pd

from cheddarbowl import config
from cheddarbowl.data_loader import apply_draft_state, available, load_draft_state, load_players
from cheddarbowl.draft.draft_context import DraftContext
from cheddarbowl.draft.recommend import score_board
from cheddarbowl.draft.tiers import assign_tiers, tier_context
from cheddarbowl.draft.vbd import compute_vbd


class Engine:
    # Value lens -> the projection column value (VBD/VONA) is measured on. Proj = expected,
    # Floor = safe/reliable, Ceil = upside. The user flips this globally to bias the board.
    LENS_COL = {"Proj": "proj_pts", "Floor": "floor", "Ceil": "ceiling"}

    def __init__(self) -> None:
        raw = load_players()
        # Precompute a static board per lens (cheap; VBD is a few sorts). Each carries its own
        # vob/tier/ranks; attribute columns (name/pos/team/floor/ceiling/adp) are identical.
        self._boards: dict = {}
        for lens, col in self.LENS_COL.items():
            players, baselines, demand = compute_vbd(raw, metric=col)
            self._boards[lens] = (assign_tiers(players, value_col=col), baselines, demand)
        # Default lens for back-compat attribute lookups (floor/ceiling/adp joins, headshots).
        self.players, self.baselines, self.demand = self._boards["Proj"]
        # sig -> (ctx, board, recs). A DICT (not one slot) because this Engine is a shared
        # @st.cache_resource: in shared/multi-viewer mode many sessions on DIFFERENT teams/lenses
        # hit it, and a single-slot memo would thrash. Cleared wholesale when it grows large.
        self._memo: dict = {}
        self._MEMO_MAX = 48

    @staticmethod
    def _state_sig(state: dict, top_n: int) -> tuple:
        """Everything evaluate() reads out of `state`, as a hashable signature. Two states with
        the same signature produce the same board, so we can reuse the last result instead of
        rescoring. Captures: the draft order (drives my slot/timing), total picks made (drives the
        on-clock pick + survival window), and each pick's (overall#, team, player, pos) — team+pos
        feed league-demand, player marks the board, overall# fixes the round."""
        picks = tuple((p.get("pick"), p.get("team_id"), str(p.get("player_id")), p.get("pos"))
                      for p in state.get("picks", []))
        return (int(top_n), tuple(state.get("draft_order") or ()),
                int(state.get("total_picks", 0)), picks)

    def evaluate(self, state: dict, top_n: int = 12, lens: str = "Proj", team_id: int | None = None):
        """Return (context, scored_board, recommendations) for a draft state.

        scored_board is the FULL available pool with p_taken/vona/score; recommendations
        is the top-N slice of it. ``lens`` (Proj/Floor/Ceil) picks which static board — i.e.
        whether value is measured on projected, floor, or ceiling points. ``team_id`` centers the
        recommendations/context on a team (per-session in shared mode); None = my team.

        Memoized on (state signature, lens, team): in Live mode this runs on every few-second
        refresh, but the draft only changes when a pick lands. When the key is unchanged we return
        the prior result (board/recs copied so callers can annotate them freely), skipping the full
        rescore — the passive refreshes in between picks become essentially free.
        """
        tid = team_id if team_id is not None else config.MY_TEAM_ID
        sig = (*self._state_sig(state, top_n), lens, int(tid))
        hit = self._memo.get(sig)
        if hit is not None:
            ctx, board, recs = hit
            return ctx, board.copy(), recs.copy()

        board_players, _, _ = self._boards.get(lens, self._boards["Proj"])
        col = self.LENS_COL.get(lens, "proj_pts")
        annotated = apply_draft_state(board_players, state)
        avail = tier_context(available(annotated), value_col=col)
        ctx = DraftContext.build(state, team_id=tid)
        board = score_board(avail, ctx)
        recs = board.head(top_n) if not board.empty else board
        if len(self._memo) >= self._MEMO_MAX:
            self._memo.clear()
        self._memo[sig] = (ctx, board, recs)
        return ctx, board, recs


def _fmt_recs(recs: pd.DataFrame) -> str:
    lines = []
    for _, r in recs.iterrows():
        lines.append(
            f"{r['rec_rank']:>2}. {r['name']:<22} {r['pos']:<3} "
            f"vob={r['vob']:6.1f} p_gone={r['p_taken']:4.0%} vona={r['vona']:5.1f}  {r['reason']}"
        )
    return "\n".join(lines)


def main() -> None:
    eng = Engine()
    state = load_draft_state()
    ctx, avail, recs = eng.evaluate(state)
    print(f"Draft: {state.get('total_picks', 0)} picks made | my slot {ctx.slot} | "
          f"window to next pick: {ctx.window} | on-clock pick #{ctx.current_pick()}")
    print(f"My roster so far: {ctx.my_counts or '(empty)'}")
    print(f"League remaining starter demand (excl. me): "
          f"{ {k: int(v) for k, v in ctx.league_demand.items() if v} }")
    print(f"\nTop recommendations:\n{_fmt_recs(recs)}")


if __name__ == "__main__":
    main()
