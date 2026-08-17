"""Strategy-engine tests, centered on the core scenario:

a player at a position the league has largely filled should be judged *less* likely to be
drafted before my next pick than an equal-value player at a scarce, in-demand position.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from cheddarbowl import config  # noqa: E402
from cheddarbowl.draft.draft_context import DraftContext, snake_pick_numbers, window_to_next_pick  # noqa: E402
from cheddarbowl.draft.pick_probability import add_pick_probability  # noqa: E402
from cheddarbowl.draft.vbd import compute_vbd, effective_demand  # noqa: E402
from cheddarbowl.draft.vona import add_vona  # noqa: E402


def _ctx(window, league_demand, total_picks=20, recent=None, my_next=None):
    return DraftContext(
        state={"total_picks": total_picks},
        slot=3, window=window,
        my_next_pick=my_next if my_next is not None else total_picks + 1 + window,
        counts={}, my_counts={},
        league_demand=league_demand,
        my_starter_need={p: 0 for p in config.POSITIONS},
        my_flex_need=0, recent_positions=recent or [],
    )


def _players(rows):
    return pd.DataFrame(rows)


# --- snake timing --------------------------------------------------------------------
def test_snake_pick_numbers_slot3():
    # 12-team, slot 3: 3, then 22 (round 2 reversal), 27, 46, ...
    assert snake_pick_numbers(3, 12)[:4] == [3, 22, 27, 46]


def test_window_alternates_on_the_clock():
    # On the clock at pick 3 -> next pick 22 -> 18 opponent picks between.
    w, nxt = window_to_next_pick({"total_picks": 2, "draft_order": []})
    assert (w, nxt) == (18, 22)
    # On the clock at pick 22 -> next 27 -> 4 between.
    w2, nxt2 = window_to_next_pick({"total_picks": 21, "draft_order": []})
    assert (w2, nxt2) == (4, 27)


# --- VBD: FLEX excludes TE this league -------------------------------------------------
def test_flex_demand_excludes_te():
    players = _players([{"pos": p, "proj_pts": 300 - i, "name": f"{p}{i}"}
                        for p in ("RB", "WR", "TE") for i in range(60)])
    demand = effective_demand(players)
    assert demand["TE"] == config.STARTERS["TE"] * config.NUM_TEAMS  # 12, no flex
    assert demand["RB"] > config.STARTERS["RB"] * config.NUM_TEAMS   # got flex
    assert demand["WR"] > config.STARTERS["WR"] * config.NUM_TEAMS
    assert (demand["RB"] - 24) + (demand["WR"] - 24) == config.FLEX_DEMAND  # all 12 flex placed


# --- THE CORE SCENARIO -----------------------------------------------------------------
def test_positional_demand_drives_pick_probability():
    """Equal-value best-available players: the scarce position is far likelier gone."""
    avail = _players([
        {"name": "QB_A", "pos": "QB", "vob": 100.0, "adp": float("nan")},
        {"name": "RB_B", "pos": "RB", "vob": 100.0, "adp": float("nan")},
    ])
    # League has filled QB (0 demand) but badly needs RB (high demand).
    ctx = _ctx(window=10, league_demand={"QB": 0, "RB": 40, "WR": 0, "TE": 0,
                                         "K": 0, "DST": 0, "FLEX": 0})
    out = add_pick_probability(avail, ctx).set_index("name")
    assert out.loc["RB_B", "p_taken"] > 0.8   # scarce -> grab now
    assert out.loc["QB_A", "p_taken"] < 0.2   # filled -> safe to wait
    assert out.loc["RB_B", "p_taken"] > out.loc["QB_A", "p_taken"]


def test_depth_target_caps():
    """Bench phase: K/DST hard-capped at 1, QB/TE allow one backup, RB/WR keep depth."""
    def ctx_for(counts):
        need = {p: max(0, config.STARTERS.get(p, 0) - counts.get(p, 0)) for p in config.POSITIONS}
        surplus = sum(max(0, counts.get(p, 0) - config.STARTERS.get(p, 0))
                      for p in config.FLEX_ELIGIBLE)
        return DraftContext(state={"total_picks": 0}, slot=3, window=0, my_next_pick=1,
                            counts={}, my_counts=counts, league_demand={},
                            my_starter_need=need,
                            my_flex_need=max(0, config.FLEX_SPOTS - surplus),
                            recent_positions=[])

    # All starters filled (RB fills FLEX -> RB count 3).
    full = ctx_for({"QB": 1, "RB": 3, "WR": 2, "TE": 1, "K": 1, "DST": 1})
    assert full.need_factor("K") == 0.0 and full.need_factor("DST") == 0.0   # never a 2nd
    assert full.need_factor("QB") > 0 and full.need_factor("TE") > 0          # 1 backup allowed
    assert full.need_factor("RB") > full.need_factor("QB")                    # RB depth > QB backup
    assert full.need_factor("WR") > full.need_factor("TE")

    # Second QB/TE already rostered -> capped, no more.
    capped = ctx_for({"QB": 2, "RB": 3, "WR": 2, "TE": 2, "K": 1, "DST": 1})
    assert capped.need_factor("QB") == 0.0 and capped.need_factor("TE") == 0.0
    assert capped.need_factor("RB") > 0                                       # RB still wanted

    # An unmet skill starter always outranks any depth need.
    early = ctx_for({"QB": 1})
    assert early.need_factor("RB") == 1.0
    # ...but streamable K/DST defer until the endgame: early they're suppressed (0.0) even with
    # the starter slot open, so their VBD doesn't crowd skill players out of the recommendations.
    assert early.need_factor("K") == 0.0 and early.need_factor("DST") == 0.0
    # Within a few picks of the end, the still-unmet K/DST starter finally surfaces.
    late = ctx_for({"QB": 2, "RB": 5, "WR": 5, "TE": 1})   # 13 picks in -> 2 left (<= defer cutoff)
    assert late.need_factor("K") == 1.0 and late.need_factor("DST") == 1.0


def test_run_momentum_raises_probability():
    """A live run at a position inflates its take probability vs. a quiet baseline."""
    avail = _players([{"name": "WR1", "pos": "WR", "vob": 80.0, "adp": float("nan")}])
    demand = {"QB": 10, "RB": 10, "WR": 10, "TE": 10, "K": 0, "DST": 0, "FLEX": 0}
    quiet = add_pick_probability(avail, _ctx(8, demand, recent=[])).iloc[0]["p_taken"]
    hot = add_pick_probability(
        avail, _ctx(8, demand, recent=["WR", "WR", "WR", "WR", "RB", "WR"])
    ).iloc[0]["p_taken"]
    assert hot > quiet


# --- VONA ------------------------------------------------------------------------------
def test_vona_flags_cliffs_and_depth():
    avail = _players([
        {"name": "TE_elite", "pos": "TE", "vob": 70.0, "p_survive": 0.0},
        {"name": "TE_cliff", "pos": "TE", "vob": 20.0, "p_survive": 0.9},  # survives
        {"name": "WR_deep1", "pos": "WR", "vob": 60.0, "p_survive": 0.9},
        {"name": "WR_deep2", "pos": "WR", "vob": 58.0, "p_survive": 0.9},  # survives, close
    ])
    out = add_vona(avail).set_index("name")
    # Elite TE: next survivor is TE_cliff -> big VONA (a real cliff).
    assert out.loc["TE_elite", "vona"] == 50.0
    # Deep WR: an almost-as-good WR survives -> tiny VONA (safe to wait).
    assert out.loc["WR_deep1", "vona"] == 2.0


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
