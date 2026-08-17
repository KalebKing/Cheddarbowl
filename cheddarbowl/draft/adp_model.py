"""ADP-spread model shared by the simulation and the roster forecast.

A player's realized draft slot is modeled as Normal(ADP, sigma(ADP)). sigma grows with
ADP (chalky at the top, chaotic deep), scaled by a `chaos` knob. Because the sim samples
from this same distribution and the forecast reads P(available) from it, the two agree by
construction.
"""

from __future__ import annotations

import math

from cheddarbowl import config


def _is_missing(adp) -> bool:
    return adp is None or (isinstance(adp, float) and math.isnan(adp))


def draft_position_sd(adp, chaos: float = 1.0) -> float:
    """Std-dev of a player's actual draft slot around their ADP (chalky->chaotic knob)."""
    if _is_missing(adp):
        return config.SIGMA_MAX * chaos
    base = min(config.SIGMA_MAX, max(config.SIGMA_MIN, config.SIGMA_K * float(adp)))
    return base * chaos


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def survival_prob(adp, pick: int, chaos: float = 1.0) -> float:
    """P(player still available at overall `pick`). No ADP -> deep, treated as available."""
    if _is_missing(adp):
        return 1.0
    sd = draft_position_sd(adp, chaos)
    return max(0.0, min(1.0, 1.0 - _phi((pick - float(adp)) / sd)))
