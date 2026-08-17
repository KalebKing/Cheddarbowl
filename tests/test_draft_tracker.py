"""Tests for the roster-diff DraftTracker: snake math + incremental pick detection."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl.ingest.legacy.nfl_scraper import DraftTracker  # noqa: E402
from cheddarbowl.ingest.legacy.parsing import PlayerRef  # noqa: E402


def P(pid: str, pos: str = "RB") -> PlayerRef:
    return PlayerRef(player_id=pid, name=f"Player{pid}", pos=pos, nfl_team="XXX")


def snap(assignments: dict[int, list[str]], num_teams: int = 4) -> dict[int, list[PlayerRef]]:
    """Build a full snapshot; teams not listed have empty rosters."""
    return {t: [P(pid) for pid in assignments.get(t, [])] for t in range(1, num_teams + 1)}


def test_snake_slot_math():
    t = DraftTracker(num_teams=4)
    # Round 1 (odd): slots 1,2,3,4 ; Round 2 (even): 4,3,2,1 ; Round 3: 1,2,3,4
    assert [t.expected_slot(p) for p in range(1, 5)] == [1, 2, 3, 4]
    assert [t.expected_slot(p) for p in range(5, 9)] == [4, 3, 2, 1]
    assert [t.expected_slot(p) for p in range(9, 13)] == [1, 2, 3, 4]
    assert [t._round_of(p) for p in (1, 4, 5, 8, 9)] == [1, 1, 2, 2, 3]


def test_learns_order_and_reconstructs_rounds():
    t = DraftTracker(num_teams=4)
    # Round 1 comes in one pick per poll: order should be learned as 1,2,3,4.
    t.update(snap({1: ["a"]}))
    t.update(snap({1: ["a"], 2: ["b"]}))
    t.update(snap({1: ["a"], 2: ["b"], 3: ["c"]}))
    t.update(snap({1: ["a"], 2: ["b"], 3: ["c"], 4: ["d"]}))
    assert t.draft_order == [1, 2, 3, 4]
    # Round 2 snake: team 4 picks first. team_on_clock(pick 5) == team 4.
    assert t.team_on_clock(5) == 4
    assert [p.round for p in t.picks] == [1, 1, 1, 1]
    assert [p.team_id for p in t.picks] == [1, 2, 3, 4]


def test_two_picks_in_one_cycle_ordered_by_snake():
    t = DraftTracker(num_teams=4, draft_order=[1, 2, 3, 4])  # order known up front
    full = {1: ["a"], 2: ["b"], 3: ["c"], 4: ["d"]}
    t.update(snap(full))
    # Now round 2: team 4 (slot-first in snake) and team 3 both picked between polls.
    t.update(snap({**full, 4: ["d", "e"], 3: ["c", "f"]}))
    r2 = [p for p in t.picks if p.round == 2]
    # Snake order for round 2 is team 4 then team 3 -> pick 5 = team4, pick 6 = team3.
    assert [(p.pick, p.team_id) for p in r2] == [(5, 4), (6, 3)]


def test_turn_double_same_team_one_poll_known_order():
    """The last team in round 1 also picks first in round 2 (the 'turn'). Both picks
    can land in a single poll and BOTH belong to the same team."""
    t = DraftTracker(num_teams=4, draft_order=[1, 2, 3, 4])
    # Round 1 picks 1-3 already in for teams 1,2,3.
    t.update(snap({1: ["a"], 2: ["b"], 3: ["c"]}))
    assert len(t.picks) == 3
    # One poll later: team 4 has drafted TWICE (its R1 pick + its R2 turn pick).
    t.update(snap({1: ["a"], 2: ["b"], 3: ["c"], 4: ["d", "e"]}))
    last_two = t.picks[-2:]
    assert [(p.pick, p.round, p.team_id) for p in last_two] == [(4, 1, 4), (5, 2, 4)]
    # Next on the clock rewinds up the snake to team 3.
    assert t.team_on_clock(6) == 3


def test_turn_double_same_team_while_learning_order():
    """Same turn-double, but the doubling team is only revealed in that poll
    (order still being learned) — it must still land as two consecutive picks."""
    t = DraftTracker(num_teams=4)  # order unknown
    t.update(snap({1: ["a"]}))
    t.update(snap({1: ["a"], 2: ["b"]}))
    t.update(snap({1: ["a"], 2: ["b"], 3: ["c"]}))
    # team 4 first appears with two players at once (its R1 + R2 turn picks).
    t.update(snap({1: ["a"], 2: ["b"], 3: ["c"], 4: ["d", "e"]}))
    assert t.draft_order == [1, 2, 3, 4]
    assert [(p.pick, p.round, p.team_id) for p in t.picks[-2:]] == [(4, 1, 4), (5, 2, 4)]


def test_no_duplicate_picks_across_polls():
    t = DraftTracker(num_teams=4)
    t.update(snap({1: ["a"]}))
    again = t.update(snap({1: ["a"]}))  # same snapshot -> nothing new
    assert again == []
    assert len(t.picks) == 1
    assert t.drafted_ids() == {"a"}


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
