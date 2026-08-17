"""Manual-mode regression: its player universe MUST share the live board's id space.

Manual picks join the board by player_id. After the ESPN migration, Manual still read the retired
NFL.com players file, whose ids don't overlap ESPN picks at all (0/637) -- silently breaking the
failsafe. These tests fail loudly if that regresses.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cheddarbowl.draft import manual
from cheddarbowl.data_loader import apply_draft_state, load_players


def test_manual_universe_matches_board_id_space():
    board_ids = set(load_players()["player_id"])
    manual_ids = set(manual.all_players()["player_id"])
    assert manual_ids, "manual universe is empty"
    # every draftable manual player must exist on the board (same id space) -- no orphan ids
    assert manual_ids <= board_ids, f"{len(manual_ids - board_ids)} manual ids not on the board"
    assert list(manual.all_players().columns) == ["player_id", "name", "pos", "nfl_team"]
    print("PASS manual_universe_matches_board_id_space")


def test_manual_pick_resolves_and_joins_board():
    board = load_players()
    pid = str(board.iloc[0]["player_id"])
    state = manual.build_state(order=list(range(1, 13)), picks=[pid])
    rec = state["picks"][0]
    assert rec["player_id"] == pid
    assert rec["player"] and rec["pos"], "manual pick did not resolve name/pos from the ESPN pool"
    annotated = apply_draft_state(board, state)
    assert bool(annotated[annotated["player_id"] == pid]["drafted"].iloc[0]), "pick didn't mark drafted"
    print("PASS manual_pick_resolves_and_joins_board")


def test_available_excludes_drafted():
    board = load_players()
    pid = str(board.iloc[0]["player_id"])
    assert pid not in set(manual.available_players([pid])["player_id"])
    print("PASS available_excludes_drafted")


if __name__ == "__main__":
    test_manual_universe_matches_board_id_space()
    test_manual_pick_resolves_and_joins_board()
    test_available_excludes_drafted()
    print("\nall manual tests passed")
