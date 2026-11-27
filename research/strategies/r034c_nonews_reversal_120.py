"""R034c: R034 eval 3, dose-response on the release-exclusion window (120 min instead of 60).

Mechanism check: if post-release repricing/drift lasts beyond an hour (Evans-Lyons order-flow
effects), excluding signals up to 120 min after a release should keep or raise edge per trade.
Chosen after seeing R034 evals 1-2 (disclosed in the run report). All other rules = r034.
"""

from __future__ import annotations

import os
import sys

from scalping.strategies.timeframe import make_timeframe

sys.path.insert(0, os.path.dirname(__file__))
from r034_nonews_reversal import R034NoNewsReversal  # noqa: E402


class R034cNoNewsReversal120(R034NoNewsReversal):
    name = "r034c_nonews_reversal_120"

    def __init__(self, window_min: int = 120, **kw):
        super().__init__(window_min=window_min, **kw)


R034cNoNewsReversal120M15 = make_timeframe(R034cNoNewsReversal120, 15)
