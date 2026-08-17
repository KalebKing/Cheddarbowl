"""Cross-season accumulation (stub): roll up data across data/seasons/* for multi-year trends."""
from __future__ import annotations

from cheddarbowl import config


def all_seasons() -> list[int]:
    """Years present under data/seasons/ (each an int)."""
    import os
    base = config.SEASONS_DIR
    if not os.path.isdir(base):
        return []
    return sorted(int(d) for d in os.listdir(base) if d.isdigit())
