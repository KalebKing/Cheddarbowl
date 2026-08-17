"""Live draft context: rosters, league-wide remaining demand, and pick timing.

Turns a draft_state dict (from the scraper) into the situational facts the probability and
recommendation models need — most importantly the **window** of opponent picks before my
next selection and how much of each position the rest of the league still needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cheddarbowl import config


def my_slot(state: dict, team_id: int = config.MY_TEAM_ID) -> int:
    """A team's 1-based draft slot: from the learned draft order if known, else config.

    Defaults to my team; pass ``team_id`` to locate any team (used by the roster forecast to
    build a projected context for an arbitrary team). Returns 0 for a team not in the order
    (and not me), which callers treat as 'no timing known'.
    """
    order = state.get("draft_order") or []
    if team_id in order:
        return order.index(team_id) + 1
    return config.DRAFT_SLOT if team_id == config.MY_TEAM_ID else 0


def snake_pick_numbers(slot: int, num_teams: int = config.NUM_TEAMS,
                       rounds: int = config.NUM_ROUNDS) -> list[int]:
    """Overall pick numbers belonging to a given slot in a snake draft."""
    picks = []
    for r in range(1, rounds + 1):
        offset = slot if r % 2 == 1 else (num_teams - slot + 1)
        picks.append((r - 1) * num_teams + offset)
    return picks


def window_to_next_pick(state: dict, team_id: int = config.MY_TEAM_ID) -> tuple[int, int | None]:
    """(opponent picks before a team's next selection, that team's next overall pick number).

    Defaults to my team. If the team is on the clock now, the window is the picks until it's up
    *again* — which is exactly the survival window the forecast needs at a projected pick.
    """
    slot = my_slot(state, team_id)
    if slot <= 0:
        return 0, None
    mine = snake_pick_numbers(slot)
    current = state.get("total_picks", 0) + 1  # the pick about to be made
    future = [m for m in mine if m >= current]
    if not future:
        return 0, None
    if future[0] == current:  # on the clock now
        return (future[1] - current - 1, future[1]) if len(future) >= 2 else (0, None)
    return future[0] - current, future[0]


def team_position_counts(state: dict) -> dict[int, dict[str, int]]:
    """{team_id: {pos: count}} from the picks made so far."""
    counts: dict[int, dict[str, int]] = {}
    for p in state.get("picks", []):
        counts.setdefault(p["team_id"], {})
        pos = p["pos"]
        counts[p["team_id"]][pos] = counts[p["team_id"]].get(pos, 0) + 1
    return counts


def _team_starter_need(counts: dict[str, int]) -> tuple[dict[str, int], int]:
    """(base starter need per position, unmet FLEX slots) for one team."""
    need = {}
    for pos, req in config.STARTERS.items():
        need[pos] = max(0, req - counts.get(pos, 0))
    surplus = sum(max(0, counts.get(pos, 0) - config.STARTERS.get(pos, 0))
                  for pos in config.FLEX_ELIGIBLE)
    flex_need = max(0, config.FLEX_SPOTS - surplus)
    return need, flex_need


def remaining_demand(state: dict, exclude_team: int | None = None) -> dict[str, float]:
    """League-wide unmet starter demand per position (+ a 'FLEX' bucket).

    This is the scarcity signal behind the core scenario: a position most teams have
    already filled has low remaining demand, so its players are unlikely to be drafted soon.
    """
    counts = team_position_counts(state)
    demand = {pos: 0.0 for pos in config.POSITIONS}
    demand["FLEX"] = 0.0
    for team in range(1, config.NUM_TEAMS + 1):
        if team == exclude_team:
            continue
        need, flex_need = _team_starter_need(counts.get(team, {}))
        for pos, n in need.items():
            demand[pos] += n
        demand["FLEX"] += flex_need
    return demand


@dataclass
class DraftContext:
    state: dict
    slot: int = 0
    window: int = 0
    my_next_pick: int | None = None
    my_team_id: int = 0        # the team this context is centered on (per-session in shared mode)
    counts: dict = field(default_factory=dict)
    my_counts: dict = field(default_factory=dict)
    league_demand: dict = field(default_factory=dict)   # excludes my team
    my_starter_need: dict = field(default_factory=dict)
    my_flex_need: int = 0
    recent_positions: list = field(default_factory=list)

    @classmethod
    def build(cls, state: dict, team_id: int = config.MY_TEAM_ID) -> "DraftContext":
        """Build a context centered on ``team_id`` (default: my team). The forecast passes a
        synthetic state (this team on the clock at a future pick, its earlier forecast picks
        consumed) with another team_id to get 'what would this team's recommender pick here'.
        In shared/multi-viewer mode, each viewer's session passes their own team_id."""
        counts = team_position_counts(state)
        window, my_next = window_to_next_pick(state, team_id)
        my_counts = counts.get(team_id, {})
        need, flex_need = _team_starter_need(my_counts)
        recent = [p["pos"] for p in state.get("picks", [])][-6:]
        return cls(
            state=state,
            slot=my_slot(state, team_id),
            window=window,
            my_next_pick=my_next,
            my_team_id=team_id,
            counts=counts,
            my_counts=my_counts,
            league_demand=remaining_demand(state, exclude_team=team_id),
            my_starter_need=need,
            my_flex_need=flex_need,
            recent_positions=recent,
        )

    def is_me(self, team_id) -> bool:
        """Is this team the one the context is centered on (the session's 'my team')?"""
        return team_id is not None and team_id == self.my_team_id

    def current_pick(self) -> int:
        return self.state.get("total_picks", 0) + 1

    def picks_until_me(self) -> int | None:
        """How many picks until I'm on the clock (0 = my pick right now). None if my
        draft is over."""
        mine = snake_pick_numbers(self.slot)
        cur = self.current_pick()
        future = [m for m in mine if m >= cur]
        return (future[0] - cur) if future else None

    def on_clock_is_me(self) -> bool:
        return self.picks_until_me() == 0

    def team_at_pick(self, overall: int) -> int | None:
        """team_id on the clock for a given overall pick, from the draft order + snake."""
        order = self.state.get("draft_order") or []
        if not order:
            return None
        idx = (overall - 1) % config.NUM_TEAMS
        rnd = (overall - 1) // config.NUM_TEAMS + 1
        slot = idx + 1 if rnd % 2 == 1 else config.NUM_TEAMS - idx
        return order[slot - 1] if 1 <= slot <= len(order) else None

    def team_on_clock(self) -> int | None:
        return self.team_at_pick(self.current_pick())

    def need_factor(self, pos: str) -> float:
        """How much I want another player at `pos`, via the depth-target model.

        - 0.0 once the position is at its roster target (config.ROSTER_TARGETS) — e.g. a
          2nd QB/TE once you have two, or ANY 2nd K/DST (target 1) → never recommended.
        - 1.0 for an unmet starter, or an open FLEX for RB/WR (starter-priority).
        - Otherwise a tapered depth appetite (config.DEPTH_WEIGHT): RB/WR high so bench
          fills with them; QB/TE low so a backup only surfaces if real value falls.
        """
        count = self.my_counts.get(pos, 0)
        target = config.ROSTER_TARGETS.get(pos, 0)
        if count >= target:
            return 0.0
        # K/DST: streamable -> defer to the endgame. Until you're within a few picks of the end of
        # your draft, don't recommend them at all (0.0), so their VBD doesn't crowd out skill players.
        if pos in ("K", "DST"):
            picks_left = config.NUM_ROUNDS - sum(self.my_counts.values())
            if picks_left > config.KDST_DEFER_UNTIL_PICKS_LEFT:
                return 0.0
        if self.my_starter_need.get(pos, 0) > 0:
            return 1.0
        if pos in config.FLEX_ELIGIBLE and self.my_flex_need > 0:
            return 1.0
        # depth phase: below target with starters filled -> taper toward the target
        starter = config.STARTERS.get(pos, 0)
        depth_total = max(1, target - starter)
        depth_remaining = target - count
        return config.DEPTH_WEIGHT.get(pos, 0.3) * (depth_remaining / depth_total)
