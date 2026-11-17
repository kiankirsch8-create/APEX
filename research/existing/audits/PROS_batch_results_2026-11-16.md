# TRULLAS · CREAMER · WILLIAMS · first 12.5-month test (Jun 2025 - 14 Jun 2026, Databento NQ trades), 2026-11-16

Code: `scalping/orderflow/newbars.py` (bar cache), `pros.py` (strategies + execution + account), `batch_pros.py` (runner).
Full trade lists: `scalping_data/batch_pros/*.csv` (not in git). Defaults for unstated rules approved by Kian 2026-11-16.

**Execution:** 1-minute bars built from trades. Stop entries fill at the level or the bar open + 1 tick. An entry level
already passed when the order could be placed counts as a **missed** trade. Stops fill 1 tick worse. Targets fill only when
traded through. If a bar touches both stop and target, the stop counts. Costs $4.50 per NQ round turn.
**Account:** $100k, risk per trade 0.25 / 0.5 / 1 / 1.5 / 2%. Size is in MNQ units, traded as NQ + MNQ remainder; the
daily loss cap is max(2%, 3 x risk). Williams trades have wide stops (0.5 x ATR10 ≈ 150 pts), so at 0.25% most of them
size below 1 MNQ and are skipped; read Williams from 0.5% up. Periods: Jun-Dec 25 research · Jan-Mar 26 · Apr-Jun 26.

## Results (per-NQ R stats; account columns = return and max drawdown)
| strategy | trades | win | avgR | t | PF | Jun-Dec 25 n/R | Jan-Mar 26 n/R | Apr-Jun 26 n/R | 0.25% | 0.5% | 1% | 1.5% | 2% | months up (1%) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| TRULLAS-1 main (1m, 38.2%) | 45 | 62.0 | 0.12 | 0.73 | 1.78 | 23/-0.00 | 14/+0.51 | 8/-0.20 | +1.1% (DD -1.7%) | +2.3% (DD -3.3%) | +4.9% (DD -6.6%) | +7.4% (DD -9.8%) | +9.8% (DD -12.9%) | 7/13 |
| TRULLAS-1 main, confluence sizing (1-4) | 45 | 62.0 | 0.12 | 0.73 | 1.78 | 23/-0.00 | 14/+0.51 | 8/-0.20 | +0.2% (DD -0.7%) | +0.8% (DD -1.5%) | +1.9% (DD -2.9%) | +2.9% (DD -4.4%) | +3.9% (DD -5.8%) | 7/13 |
| TRULLAS-1 target 50% | 45 | 58.0 | 0.201 | 1.14 | 1.83 | 23/+0.01 | 14/+0.64 | 8/-0.03 | +2.0% (DD -1.4%) | +4.1% (DD -2.8%) | +8.8% (DD -5.5%) | +13.3% (DD -8.2%) | +17.9% (DD -10.8%) | 7/13 |
| TRULLAS-1 target 61.8% | 44 | 50.0 | 0.227 | 1.11 | 1.86 | 22/-0.06 | 14/+0.70 | 8/+0.20 | +2.2% (DD -2.2%) | +4.6% (DD -4.3%) | +9.6% (DD -8.5%) | +14.4% (DD -12.5%) | +19.6% (DD -16.3%) | 8/13 |
| TRULLAS-1 5m bars (38.2%) | 12 | 42.0 | -0.402 | -1.81 | 0.2 | 9/-0.20 | 1/-1.02 | 2/-1.02 | -1.1% (DD -1.4%) | -2.3% (DD -2.9%) | -4.6% (DD -5.9%) | -7.0% (DD -8.9%) | -9.3% (DD -11.8%) | 3/8 |
| TRULLAS-1 5m bars (61.8%) | 12 | 25.0 | -0.436 | -1.42 | 0.2 | 9/-0.24 | 1/-1.02 | 2/-1.02 | -1.1% (DD -1.6%) | -2.4% (DD -3.4%) | -5.0% (DD -6.8%) | -7.6% (DD -10.1%) | -10.1% (DD -13.4%) | 2/8 |
| TRULLAS-1 no trend filter | 110 | 61.0 | 0.067 | 0.68 | 1.34 | 54/-0.16 | 35/+0.32 | 21/+0.24 | +1.0% (DD -3.0%) | +2.9% (DD -5.8%) | +6.3% (DD -11.4%) | +9.3% (DD -16.8%) | +12.3% (DD -22.0%) | 7/13 |
| TRULLAS-2 sweep (50%) | 77 | 39.0 | -0.072 | -0.47 | 0.99 | 43/-0.29 | 18/+0.37 | 16/+0.00 | -1.8% (DD -2.9%) | -3.0% (DD -6.2%) | -6.1% (DD -12.4%) | -9.9% (DD -18.1%) | -13.3% (DD -23.5%) | 6/13 |
| TRULLAS-2 sweep (66%) | 78 | 33.0 | 0.011 | 0.06 | 1.07 | 43/-0.21 | 19/+0.45 | 16/+0.10 | -0.4% (DD -2.5%) | -0.3% (DD -5.4%) | -0.4% (DD -10.7%) | -1.6% (DD -16.1%) | -2.2% (DD -20.8%) | 7/13 |
| CREAMER-1 reversal | 0 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| CREAMER-1 reversal loose (any opposite delta) | 2 | 0.0 | -0.856 |  | 0.0 | 2/-0.86 | 0 | 0 | -0.4% (DD -0.4%) | -0.8% (DD -0.8%) | -1.6% (DD -1.6%) | -2.5% (DD -2.5%) | -3.3% (DD -3.3%) | 0/1 |
| CREAMER-1 reversal loosest (+ no VA / POC rule) | 10 | 30.0 | -0.191 | -0.54 | 0.58 | 6/-0.12 | 2/-0.65 | 2/+0.07 | -0.5% (DD -0.8%) | -1.0% (DD -1.5%) | -1.8% (DD -3.2%) | -3.0% (DD -4.9%) | -3.9% (DD -6.4%) | 2/7 |
| CREAMER-1 continuation | 57 | 39.0 | 0.096 | 0.51 | 1.3 | 42/+0.14 | 9/-0.35 | 6/+0.48 | +0.7% (DD -2.1%) | +2.0% (DD -4.0%) | +4.7% (DD -8.0%) | +6.4% (DD -11.9%) | +8.4% (DD -15.7%) | 6/12 |
| WILLIAMS-1 close-open (down close) | 102 | 48.0 | 0.161 | 1.31 | 1.46 | 52/-0.04 | 34/+0.28 | 16/+0.55 | -0.4% (DD -1.1%) | +5.7% (DD -2.1%) | +13.2% (DD -4.9%) | +22.9% (DD -7.7%) | +30.5% (DD -10.1%) | 8/13 |
| WILLIAMS-1 close-open (up close, control) | 150 | 51.0 | 0.018 | 0.22 | 1.14 | 87/-0.03 | 28/-0.12 | 35/+0.24 | -0.2% (DD -1.3%) | -0.3% (DD -4.2%) | +0.9% (DD -8.7%) | +0.1% (DD -13.2%) | +2.0% (DD -17.6%) | 7/13 |
| WILLIAMS-1 day of week (Mon/Tue/Thu) | 153 | 50.0 | 0.051 | 0.6 | 1.27 | 85/-0.07 | 37/+0.11 | 31/+0.31 | -1.1% (DD -2.0%) | +0.7% (DD -5.4%) | +3.2% (DD -10.5%) | +5.8% (DD -15.1%) | +8.7% (DD -19.9%) | 7/13 |
| WILLIAMS-1 control: buy every open | 252 | 50.0 | 0.076 | 1.09 | 1.28 | 139/-0.03 | 62/+0.10 | 51/+0.34 | -0.6% (DD -2.0%) | +5.7% (DD -4.8%) | +12.7% (DD -10.1%) | +21.0% (DD -14.2%) | +32.6% (DD -19.0%) | 7/13 |
| WILLIAMS-1 blastoff | 47 | 53.0 | 0.176 | 1.14 | 1.21 | 25/+0.19 | 12/+0.36 | 10/-0.09 | +1.4% (DD -0.4%) | +4.2% (DD -1.2%) | +7.5% (DD -3.4%) | +12.5% (DD -5.1%) | +16.0% (DD -7.0%) | 7/12 |
| WILLIAMS-1 expansion hold-to-close | 43 | 51.0 | 0.075 | 0.57 | 1.18 | 22/+0.06 | 13/-0.10 | 8/+0.40 | +0.1% (DD -0.5%) | +1.1% (DD -1.0%) | +3.0% (DD -2.1%) | +3.7% (DD -3.3%) | +5.7% (DD -5.0%) | 7/13 |
| TRULLAS-1 fast swings (3-bar, 38.2%) | 88 | 60.0 | 0.059 | 0.56 | 0.92 | 51/+0.07 | 16/-0.02 | 21/+0.09 | +0.9% (DD -1.7%) | +2.2% (DD -3.4%) | +4.4% (DD -6.9%) | +6.7% (DD -10.2%) | +8.5% (DD -13.5%) | 7/13 |
| TRULLAS-1 fast swings (3-bar, 61.8%) | 89 | 56.0 | 0.295 | 2.25 | 1.4 | 52/+0.32 | 16/+0.19 | 21/+0.31 | +6.1% (DD -1.5%) | +13.2% (DD -3.1%) | +28.4% (DD -6.1%) | +45.2% (DD -9.0%) | +63.5% (DD -11.8%) | 8/13 |

## Reading
- **Nothing is statistically proven** (only one |t| > 2, see below). NQ rose strongly in this window: "buy every open,
  exit at the close" makes +12.7% at 1%. Long-biased day trades must beat that control, not zero.
- **TRULLAS-1 (fast swings, 61.8% target)** is the best result: 89 trades, +0.30R, t = 2.25, positive in all three
  periods, 8/13 months up. It is **fragile**:
  - The median stop is 9 points. One extra tick of slippage per side gives +0.22R (t = 1.7), two ticks +0.15R (t = 1.1),
    four ticks ~0.
  - It was picked after seeing 8 Trullas variants (multiple testing).
  - The main default (5-bar swings) gave 45 trades, +0.12R, t = 0.7. Most signals were missed because price passes the
    23.6% entry before a 5-bar swing is confirmed.
  - Next step: a tick-level replay of these trades (real fills) and an out-of-sample year before any trust.
- **TRULLAS-1 regular vs hidden:** hidden divergences did better in every Trullas variant (e.g. +0.37R vs +0.24R).
  Confluence sizing (1-4 contracts) didn't help: confluence 1 trades were as good as 3.
- **TRULLAS-1 on 5m bars:** 12 trades, negative. **TRULLAS-2 opening sweep:** ~0R (50%: -0.07R, 66%: +0.01R).
- **CREAMER-1 reversal:** his full rule (strong opposite delta + candle POC in the wick + close the other way, in a fib
  zone outside value) **never fired** in 12.5 months. Loosened versions: 2 and 10 trades, negative.
  - Like VALENTINI-3, the stated pattern is too rare on 5m NQ as we define it.
  - Missing context that may matter: gamma (A2), the path indicator (C1), course part 2.
- **CREAMER-1 continuation:** 57 trades, +0.10R, t = 0.5 → inconclusive.
- **WILLIAMS-1:**
  - **Close-open:** "down close + higher open" = 57 trades, +0.27R; the "up close" control = +0.02R. That's the direction
    he describes, but t = 1.3 overall and it's long-only in a bull year.
  - **Day of week:** Monday strongest (+0.31R), Thursday weakest (-0.14R). His S&P list (Mon/Tue/Thu) only half carries
    over to NQ.
  - **Blastoff:** 47 trades, +0.18R, 7/12 months up, small drawdowns. Promising, but small (t = 1.1).
  - **Expansion hold-to-close:** +0.08R, flat.

## Next (needs Kian)
1. TRULLAS-1 fast swings: tick-level fill replay + cost stress, then hold it out on new data (Jun 2026 → now) before any demo.
2. WILLIAMS blastoff and close-open: needs more history (e.g. 2018-2024 NQ daily/1m) to reach a useful sample; these are
   daily patterns, 1 trade/day at most.
3. CREAMER: decide whether to buy gamma history or drop the reversal model as "not testable as stated".

## Tick replay of TRULLAS-1 fast swings / 61.8% (2026-11-17) — `scalping/orderflow/replay_trullas.py`
The same signals are replayed on the real trade tape: fill = first trade through the level, the stop fills at the trade that
hits it, the target needs a trade through by 1 tick, and one position at a time.

| Slippage per side | Fills | Win | Avg R | t | Jun-Dec 25 | Jan-Mar 26 | Apr-Jun 26 |
|---|---|---|---|---|---|---|---|
| 0 ticks | 125 | 49% | +0.087 | 0.83 | +0.07 | +0.02 | +0.18 |
| 1 tick | 125 | 49% | +0.030 | 0.29 | +0.01 | -0.02 | +0.12 |
| 2 ticks | 125 | 49% | -0.022 | -0.22 | -0.05 | -0.06 | +0.07 |

**Verdict: the +0.30R bar result was mostly a 1-minute-bar artefact.** On real ticks the edge is ~0 after realistic
slippage. The bar model hid how often the tight stop (median 9 pts) is hit inside the minute, and it skipped some fills.
TRULLAS-1 stays in the library as "no edge found as interpreted"; it's still worth a forward demo only as a curiosity.


## Live engine parity (2026-11-17)
The demo engine (`scalping/live/`) replays raw ticks and re-runs the unchanged backtest functions. On 2026-05-11/13/18 every
strategy's live trades equal the backtest trades (entry second, fill, exit). Bugs found and fixed on the way: orders created
on the last closed bar were dropped in live mode (Trullas, Creamer); re-issue guard marked every order; Williams partial-view
orders started a minute late; blastoff brackets were off the tick grid (backtest prices change by < 1 tick).
