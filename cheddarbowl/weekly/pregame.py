"""Weekly PRE-game analysis (stub).

Matchup projections, start/sit, waiver targets for the upcoming week. Reads
`config.weekly_dir(week)` inputs and writes a pregame summary there.
"""
from __future__ import annotations

from cheddarbowl import config


def build(week: int, year: int = config.SEASON) -> dict:
    raise NotImplementedError("weekly pregame analysis not built yet")
