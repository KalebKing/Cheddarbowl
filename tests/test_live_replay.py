"""End-to-end regression for the LIVE draft path.

A synthesized full snake draft is fed through the SAME parser the CDP capture uses (Draft.handle),
then the reconstructed state is written and re-read through the app's loader. This locks the whole
attach -> parse -> write -> load pipeline against future edits, complementing the frame-level unit
tests in test_draft_tracker.py. No live draft or recorded capture needed -- the frame stream is
generated deterministically from the known websocket protocol.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl import config
from cheddarbowl.ingest.espn_live import Draft, _snake
from cheddarbowl.data_loader import load_draft_state

N = config.NUM_TEAMS       # 12
R = config.NUM_ROUNDS      # 15
ORDER = list(range(1, N + 1))      # team i sits in draft slot i


def _round_team_sequence(rnd: int) -> list:
    """Teams in the order they pick this round (snake: forward on odd rounds, reversed on even)."""
    return ORDER if rnd % 2 == 1 else list(reversed(ORDER))


def _synth_frames():
    """Return (frames, expected).

    frames: the true-order SELECTING/SELECTED text stream a draft room emits.
    expected: overall pick# -> (team_id, round, player_id).
    D/ST-style negative playerIds appear in the last two rounds to exercise that path.
    """
    frames, expected = [], {}
    pid = 1000
    for rnd in range(1, R + 1):
        for team in _round_team_sequence(rnd):
            pid += 1
            player = str(-pid) if rnd >= R - 1 and team % 6 == 0 else str(pid)   # some negative ids
            frames.append(f"SELECTING {team} 30000")
            frames.append(f"SELECTED {team} {player} 20")
            expected[_snake(team, rnd, N)] = (team, rnd, player)                 # slot == team here
    return frames, expected


def _feed(d: Draft, frames) -> None:
    for f in frames:
        d.handle(f)


def test_full_snake_reconstructs_exactly():
    d = Draft(log=False)
    d.set_order(ORDER)
    frames, expected = _synth_frames()
    _feed(d, frames)

    st = d.state()
    assert st["total_picks"] == N * R
    assert sorted(d.by_overall) == list(range(1, N * R + 1))          # contiguous 1..180
    for rec in st["picks"]:
        team, rnd, player = expected[rec["pick"]]
        assert rec["team_id"] == team
        assert rec["round"] == rnd
        assert rec["player_id"] == player
        assert rec["pick"] == _snake(team, rnd, N)                    # snake-consistent numbering
    assert len(d.drafted) == N * R                                    # no duplicate players
    print("PASS full_snake_reconstructs_exactly")


def test_replay_is_idempotent():
    """Re-feeding the whole capture (crash-recovery / frame re-delivery) must not double-count."""
    d = Draft(log=False)
    d.set_order(ORDER)
    frames, _ = _synth_frames()
    _feed(d, frames)
    _feed(d, frames)                                                  # replay the entire stream again
    assert d.state()["total_picks"] == N * R                          # dedup by player_id -> no growth
    print("PASS replay_is_idempotent")


def test_order_learned_from_clock_when_unlocked():
    """With no REST order, the slot order must be learned from the SELECTING clock sequence."""
    d = Draft(log=False)                                              # no set_order()
    frames, _ = _synth_frames()
    _feed(d, frames)
    assert d.slot_order[:N] == ORDER                                  # round-1 clock order == slots
    assert d.state()["total_picks"] == N * R
    print("PASS order_learned_from_clock_when_unlocked")


def test_write_load_roundtrip_and_defensive_read():
    """state -> atomic write -> loader round-trips exactly; a corrupt file never raises."""
    d = Draft(log=False)
    d.set_order(ORDER)
    frames, _ = _synth_frames()
    _feed(d, frames)

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "draft_state.json")
        Draft._atomic_write(path, d.state())
        loaded = load_draft_state(path)
        assert loaded["total_picks"] == N * R
        assert loaded["picks"] == d.state()["picks"]                  # exact round-trip

        # Defensive read: a corrupt file must NOT crash the app mid-draft (holds last-good instead).
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        got = load_draft_state(path)
        assert isinstance(got, dict) and "picks" in got
    print("PASS write_load_roundtrip_and_defensive_read")


def _draft_with_missed_pick():
    """Feed the first 4 rounds but DROP team 1's round-3 SELECTED frame -- simulating a websocket
    miss. That shifts team 1's later picks down a round (each team's Nth SELECTED is numbered round
    N). Returns (draft, true_map) where true_map[overall] = (team, player) is REST's ground truth."""
    d = Draft(log=False)
    d.set_order(ORDER)
    pid = 1000
    true_map = {}
    for rnd in range(1, 5):
        for team in _round_team_sequence(rnd):
            pid += 1
            player = str(pid)
            true_map[_snake(team, rnd, N)] = (team, player)
            d.handle(f"SELECTING {team} 30000")
            if team == 1 and rnd == 3:
                continue                                     # <-- the missed SELECTED frame
            d.handle(f"SELECTED {team} {player} 20")
    return d, true_map


def test_reconcile_heals_missed_pick_and_shift():
    d, true_map = _draft_with_missed_pick()
    missed_overall = _snake(1, 3, N)
    missed_player = true_map[missed_overall][1]
    assert missed_player not in d.drafted                    # we genuinely lost it

    fixed = d.reconcile(true_map)                             # REST authoritative, contiguous 1..48
    assert fixed >= 1
    for overall, (team, player) in true_map.items():         # every pick now matches ground truth
        rec = d.by_overall[overall]
        assert rec["team_id"] == team
        assert rec["player_id"] == player
        assert rec["round"] == (overall - 1) // N + 1
    assert missed_player in d.drafted                        # recovered
    assert d.team_round[1] == 4                               # counters resynced (team 1 has 4 picks)
    print("PASS reconcile_heals_missed_pick_and_shift")


def test_reconcile_is_noop_without_authoritative_rest():
    """When REST hasn't populated playerIds (all -1 -> empty map), reconcile must not touch a thing."""
    d, _ = _draft_with_missed_pick()
    before = dict(d.by_overall)
    assert d.reconcile({}) == 0
    assert d.by_overall == before
    print("PASS reconcile_is_noop_without_authoritative_rest")


def test_reconcile_only_acts_on_contiguous_prefix():
    """A gappy REST (pick 4 still -1) must only heal the run contiguous from #1, never past the gap."""
    d = Draft(log=False)
    d.set_order(ORDER)
    rest = {1: (1, "2001"), 2: (2, "2002"), 3: (3, "2003"), 5: (5, "2005")}   # missing #4
    assert d.reconcile(rest) == 3                             # only 1..3
    assert set(d.by_overall) == {1, 2, 3}
    assert 5 not in d.by_overall
    print("PASS reconcile_only_acts_on_contiguous_prefix")


def test_apply_overrides_wins_and_is_idempotent():
    """An in-live manual override replaces the pick at its overall# and re-applies idempotently."""
    import json as _json
    d = Draft(log=False)
    d.set_order(ORDER)
    frames, _ = _synth_frames()
    _feed(d, frames)                                     # full 180-pick board
    orig = dict(d.by_overall[5])
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "pick_overrides.json")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"5": {"team_id": orig["team_id"], "player_id": "999999"}}, f)
        saved = config.PICK_OVERRIDES_JSON
        config.PICK_OVERRIDES_JSON = path                # scraper reads this exact path
        try:
            n1 = d.apply_overrides()
            n2 = d.apply_overrides()                      # already applied -> no-op
        finally:
            config.PICK_OVERRIDES_JSON = saved
    assert n1 == 1 and n2 == 0
    assert d.by_overall[5]["player_id"] == "999999"      # the fix won over the websocket pick
    assert "999999" in d.drafted
    assert orig["player_id"] not in d.drafted            # the replaced player is gone
    assert d.state()["total_picks"] == N * R             # count unchanged (same slot replaced)
    print("PASS apply_overrides_wins_and_is_idempotent")


if __name__ == "__main__":
    test_full_snake_reconstructs_exactly()
    test_replay_is_idempotent()
    test_order_learned_from_clock_when_unlocked()
    test_write_load_roundtrip_and_defensive_read()
    test_reconcile_heals_missed_pick_and_shift()
    test_reconcile_is_noop_without_authoritative_rest()
    test_reconcile_only_acts_on_contiguous_prefix()
    test_apply_overrides_wins_and_is_idempotent()
    print("\nall live-replay tests passed")
