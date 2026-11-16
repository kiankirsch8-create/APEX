"""R012b: R012 with a 4-pip trigger instead of 8.

Decided BEFORE any R012 outcome was seen: a one-quarter frequency check (2019Q2) found
only 1 of 260 release windows with a >= 8-pip 3-minute move (9 with >= 5 pips), so the
registered 8-pip trigger cannot reach the 200-trade gate over 2018-21. Everything else
(release times, entry at T+3 min, SL max(8 pips, |move|), 60-min hold) is unchanged.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from r012_release_continuation import R012ReleaseContinuation  # noqa: E402


class R012bReleaseContinuation4Pip(R012ReleaseContinuation):
    name = "r012b_release_continuation_4pip"

    def __init__(self, min_move_pips: float = 4.0, **kw):
        super().__init__(min_move_pips=min_move_pips, **kw)
