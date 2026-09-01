# APEX Shadow Spec — Funded Guardrails (Backtester)

Parallel shadow curves simulate funded CFG guardrails without altering the real
chrono backtest curve. The real run stays **unguarded** so headline results stay
comparable to baseline jobs (e.g. `17a955c0`, `1e246e2b`).

## SHADOW_GUARD_CONFIGS (23)

Each entry is a partial or full funded guardrail stack. Unspecified fields default
to no-op (multiplier 1.0 / disabled).

| ID | warmup | cold start | DD ladder | loss cap | daily stop |
|----|--------|------------|-----------|----------|------------|
| `ramp20_050` | 20d @ 0.50 | — | — | — | — |
| `ramp20_025` | 20d @ 0.25 | — | — | — | — |
| `ramp40_050` | 40d @ 0.50 | — | — | — | — |
| `ramp40_025` | 40d @ 0.25 | — | — | — | — |
| `ramp60_025` | 60d @ 0.25 | — | — | — | — |
| `cold3_025` | — | 3 @ 0.25 | — | — | — |
| `cold5_025` | — | 5 @ 0.25 | — | — | — |
| `cold5_050` | — | 5 @ 0.50 | — | — | — |
| `cold10_025` | — | 10 @ 0.25 | — | — | — |
| `dd5_050` | — | — | 5%→0.50 | — | — |
| `dd8_025` | — | — | 8%→0.25 | — | — |
| `dd_ladder` | — | — | 5%→0.50, 8%→0.25 | — | — |
| `cap100` | — | — | — | 1.0% | — |
| `cap150` | — | — | — | 1.5% | — |
| `daily2` | — | — | — | — | 2.0% |
| `daily3` | — | — | — | — | 3.0% |
| `daily5` | — | — | — | — | 5.0% |
| `ramp40_cold5` | 40d @ 0.25 | 5 @ 0.25 | — | — | — |
| `ramp40_ladder` | 40d @ 0.25 | — | 5/8% ladder | — | — |
| `cold5_ladder` | — | 5 @ 0.25 | 5/8% ladder | — | — |
| `stack_noramp` | — | 5 @ 0.25 | ladder | 1.5% | 3.0% |
| `stack_nocold` | 40d @ 0.25 | — | ladder | 1.5% | 3.0% |
| `full_stack` | 40d @ 0.25 | 5 @ 0.25 | ladder | 1.5% | 3.0% |

## SHADOW_COMPOUND_RISK_PCTS

Independent capital curves at fixed account risk percentages:

`[0.10, 0.15, 0.20, 0.30, 0.50]`

Each runs **bare** (`compound_NN_bare`) and with **`full_stack`** guardrails
(`compound_NN_full`).

## Per-curve state

Each shadow configuration maintains its own:

- capital, peak capital, day-anchor capital, day P&L
- daily-loss-stop state
- A+B health histories (`_STRAT_PNL_HISTORY`, `_ST_MEDIUM_HISTORY` equivalents)

Blocked trades do **not** enter that config's A+B histories.

## Trade record fields

```json
"shadow_guard": {
  "ramp40_025": {"mult": 0.25, "pnl": 12.34, "blocked": false},
  "full_stack": {"mult": 0.06, "pnl": 2.96, "blocked": false}
}
```

Compound curves are written to `shadow_compound` on each trade.

## Completion summaries

`shadow_guard_summary` and `shadow_compound_summary` per config:

- final_capital, peak_capital, max_drawdown_pct, worst_day_pct
- positive_months, trades_taken, trades_blocked
