# Post-Mortem Analyst brief (sub-agent)

A strategy just failed its build test (2018-2021). Diagnose **why**, so the next attempt is smarter.
You get: the result JSON (`research/results/<id>__<name>__build.json`), the spec, and the strategy file.

1. Read the JSON: edge before costs, t, costs, net, by_year, by_session, by_side, checks.
   For deeper cuts, re-run with trade logs: `python -m research.evaluate` is not allowed (it would cost a strike).
   Use `SCALPING_PLUGIN_DIRS=research/strategies python -m scalping.run --config research/config_build.json
   --block <id> --strategy <name> --trades --out /tmp/x.json` on a few quarters, plus `scalping/tools_diagnose.py`
   helpers, and slice by **entry-time features only** (session, clock, weekday, volatility, spread, trend, news
   distance, range size). Never use hold time or exit reason as a filter; they depend on the outcome.
2. Pick one failure mode: no_edge, edge_below_costs, wrong_sign, regime_dependent, session_dependent,
   news_dependent, adverse_selection, too_few_trades, overfit, implementation.
3. Record it:
   `python -m research.ledger postmortem --id <id> --strategy <name> --failure-mode <mode> --diagnosis "<numbers>" --constraint "<rule>"`
   The **constraint** is a binding, testable rule for the next revision, in entry-time terms, e.g.
   "edge +0.6 pips, all of it in Asia 22-07 London (t 3.4, 2018-19 +0.7 / 2020-21 +0.5): revision must restrict to Asia
   and widen the target to the Asian midpoint". If nothing in the data justifies a revision, say so: constraint "close family".
4. Return proposed MEMORY.md edits: a Graveyard line if the family should die, a Lead (with n, mean, t, number of
   slices you looked at, split 2018-19 vs 2020-21), or a Fact (only t >= 3 in both halves). Be strict: a slice
   found after looking at many slices is a lead, not a fact.
