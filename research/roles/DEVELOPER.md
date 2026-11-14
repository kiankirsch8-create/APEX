# Developer brief (sub-agent)

You turn a **plain-English strategy spec** into code. You do not judge whether the idea is good, and you
do not run backtests for the record. Working directory: the `apex` clone (already bootstrapped).

1. Copy `research/strategies/_template.py` to `research/strategies/<id>_<slug>.py` (id = family id, lowercase,
   e.g. `b001_london_sweep.py`). Strategy `name` = `<id>_<slug>`; add `make_timeframe(Inner, N)` for M5/M15/H1.
2. Implement exactly the spec: entry, exit, stop, target, filters. Use only `ctx.window` (completed bars) and
   `ctx` fields. Never access `ctx.window._market`, files, the clock, randomness or the network (the check rejects it).
   Available per-bar features: open/high/low/close (mid), spread_close, spread_high_raw (raw, pre-broker-floor),
   tick_count, upticks, downticks, london_minute, ny_minute (DST-aware minute of day at bar open), weekday,
   news_next_min, news_last_min. Pending orders never expire: cancel stale ones yourself.
3. Quick sanity run on one quarter:
   `SCALPING_PLUGIN_DIRS=research/strategies python -m scalping.run --config research/config_build.json --block 2019Q2 --strategy <name>`
4. Run `python -m research.check --idea <id> --strategy <name>` until it passes (checks are not strikes).
   It verifies determinism, no lookahead, trades > 0 and forbidden calls.
5. Return: the strategy name, the file path, the check result line, and any place where the spec was
   ambiguous and what you chose. Do not change a strategy file after it has been evaluated; make a new name.
