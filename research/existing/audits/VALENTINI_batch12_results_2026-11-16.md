# VALENTINI suite · 12.5-month test (Jun 2025 - 14 Jun 2026, Databento NQ trades), 2026-11-16

Harness `scalping/orderflow/valentini2.py`, runners `batch12.py` / `batch12_core.py`. Full tables:
`scalping_data/batch12/summary.csv`, `scalping_data/batch12core/summary.csv` (not in git). Per contract NQ after $4.50 costs + 1 tick
slippage; account = $100k, MNQ sizing to 0.25% risk, 2% daily cap. Checks: baseline reproduces earlier Jun-Aug results exactly;
chunked run identical to standalone for Jun-Aug; no look-ahead (earlier truncation test, same engine).
Note: the planned holdout (Apr-Jun 2026) is inside this run (Kian asked for 12 months); results are split by period instead.

Rule sets: A baseline · B effort-vs-result triggers · C RTH profile-framing bias · D his management (trail behind aggression,
break-even session stop) · E 40-tick range bars · F all of B-E together.

## Best results (none statistically proven: best t ≈ 1.5)
| Model / set | Trades | Avg R | PF | Net / NQ | $100k | Research / Validation / Holdout avg R |
|---|---|---|---|---|---|---|
| V1, F all his rules | 88 | +0.36 | 1.50 | +$6,134 | +6.1% (DD 3.5%) | +0.20 / +0.41 / +0.45 |
| V1, C profile bias | 31 | +0.96 | 2.09 | +$5,836 | +6.7% (DD 3.3%) | +0.78 / -1.03 / +4.40 |
| V1, A baseline | 59 | +0.30 | 1.30 | +$3,084 | +3.1% | +0.36 / -0.47 / +1.14 |
| V4, E range bars | 120 | +0.12 | 1.38 | +$7,450 | +2.8% | +0.15 / -0.09 / +0.23 |
| V4, A baseline | 61 | +0.15 | 1.02 | +$266 | +2.0% | +0.23 / +0.29 / -0.10 |
| CORE V1+V4, E range bars (one position) | 211 | +0.17 | 1.36 | +$12,130 | +5.8% (DD 6.2%) | +0.39 / -0.49 / +0.23 |
| CORE V1+V4, F all his rules | 128 | +0.18 | 1.03 | +$519 | +3.7% (DD 3.7%) | -0.16 / +0.27 / +0.30 |
| Separate accounts V1(F)+V4(E)+V2(D) | 227 | +0.20 | – | +$13,733 | +8.6% (DD 5.4%), 9/13 months up | selected on all data → optimistic |

## Did not work
- V8 value migration (Effort layer missing): 861 trades, -0.22R (1m); best -0.06R (F). Negative in every period → not usable as a proxy.
- V7 IVB (target 1R, breakout candle filter): 21 trades +0.04R (1m); no filter 126 trades -0.14R; pullback -0.26R (1m), ~0 on range bars.
  The proprietary protection-level target is missing.
- V2 London: flat in every set (19-60 trades).
- V3 AAA: 0-1 trades in 12.5 months under every set.
- V1 Delta Print: inconsistent (best B +0.20R / 96 trades, negative on range bars).
- Effort triggers: help V1 Delta Print, hurt V4 and V2. His management (as interpreted): cuts V1/V4 winners, helps only V2.
- "ALL" with V8 in the suite: -52% account (V8 takes the single position slot all day).

## Reading
V1 is the only model positive in all six sets; V1 with all his rules is the steadiest (positive in all three periods).
V4 works on 40-tick range bars. Validation Jan-Mar 2026 is the weak period for most models, which matches his own account of
2025-26 being a bad regime for pure trend models. Sample sizes (30-210 trades) are far below his 300+ out-of-sample rule (V-f).
