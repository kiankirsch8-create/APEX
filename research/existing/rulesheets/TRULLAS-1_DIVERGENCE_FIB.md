# TRULLAS-1 · Divergence + Fibonacci (MACD / RSI / volume) on NQ — rule sheet v1
# TRULLAS-2 · Opening sweep + volume divergence → Dow 2/3 rebound — summary

Creator: David Trullas (Life in Trading; physio/osteopath; Robbins World Cup quarterly futures winner, 700-1321%/quarter at
championship leverage). Market: NQ mini/micro intraday around the US open (also stocks/crypto swing). Bias: long with the
macro trend; shorts only when very clear. Sources: `sources/trullas_*_TRANSCRIPT.md`, `sources/trullas_pasted_in_chat_NOTES.md` (D = Rankia talk).

## TRULLAS-1 rules
| # | Rule | Source | Status |
|---|---|---|---|
| T1 | MACD divergence is mandatory ("si MACD no me da la imagen... no opero") | D | STATED; MACD settings **MISSING** (default 12/26/9 → INTERPRETED) |
| T2 | Regular divergence: new high (low) with MACD lower (higher); add RSI and bar volume at the extreme lower than at the prior extreme | D, divergence_fib_qna, helite | STATED; RSI period **MISSING** |
| T3 | Hidden divergence: higher low in price, indicators lower low → long (mirror for shorts) | D | STATED |
| T4 | Which two extremes: "cada uno coge los mínimos que quiere" | simple_50_years | **DISCRETIONARY** → INTERPRETED (consecutive swing pivots) |
| T5 | Fib from the newest extreme to the opposite extreme between the two swings (e.g. highest high between the two lows) | D | STATED |
| T6 | Entry: stop order 1 tick beyond the ~23-25% retracement level; stop 1 tick beyond the extreme | D | STATED |
| T7 | Targets 1 tick before: 38.2% (beginner standard), 50%, 61.8%/66% (Dow 2/3); exits slightly before levels | D, 50_years, divergence_fib_qna | STATED; which target when → INTERPRETED (variants) |
| T8 | Size = confluence count: MACD 1, +RSI 2, +RSI out of overbought/oversold 3, +volume 4 contracts | D | STATED |
| T9 | Timeframes: daily + 1h context (key levels/fibs), 5m bigger moves, 1m/30s signals, 5s execution for tighter stops; "same method on any timeframe" | setup, helite | STATED |
| T10 | With the higher-timeframe trend only (long in NQ uptrend) | several | STATED; trend definition → INTERPRETED |

## TRULLAS-2 (opening sweep)
At the US open price whips lower, makes a new low with lower volume than the prior low → long; targets 50% then 66% of the
move from the reference high to the low; avoid targets inside a "mazacote" (horizontal high-volume zone); scale in only as price
runs (stop to break-even fast); never let a winner turn into a loss. STATED, levels partly discretionary.

## Context / filters he uses
- S/R as clusters (5-20 pts) from prior highs/lows/opens/closes drawn before the session; breakouts need volume, not a poke.
- Hollow candles (big body, little volume inside) get refilled later ("osmosis"); rollover gap tends to fill; trade the contract with more volume in rollover week.
- Delta alone is unreliable (~1/3 of days disagree); big resting book orders as magnets/targets (needs order-book data → MISSING).
- No news trading; done by ~16:00 CET; 0-25 trades/day; daily loss = 2-3 losing "bullets".

## Verdict
**Partial, but the most testable new creator for NQ.** T1-T3, T5-T8 are mechanical on bars we can build from our trade data
(MACD/RSI/bar volume). Missing: indicator settings (T1/T2), swing selection (T4), his proprietary "Helite" details (3 registered
methods, entries partly withheld). An SMA 6/70/200 cross he showed for daily stocks was backtested by a third party: few trades,
mixed results → not his full method.

## First 12.5-month test (2026-11-16)
See `../audits/PROS_batch_results_2026-11-16.md` (defaults approved by Kian; code `scalping/orderflow/pros.py`).
