# WILLIAMS-1 · Day-trading bias toolkit (expansion days, hold to close, close-open + weekday bias) — rule sheet v1
# WILLIAMS-2 · Swing/position (COT + seasonals + cycles → daily trend change) — summary, swing-engine candidate

Creator: Larry Williams (Robbins World Cup 1987, +11,376%, ~30% risk/trade). Sources (`sources/`):
TG = `williams_titans_godfather_TRANSCRIPT.md`, TL = `williams_traderlion_interview_TRANSCRIPT.md`,
NT = `williams_ninjatrader_live_TRANSCRIPT.md`, CL = `williams_clips_from_chat_TRANSCRIPT.md` + `williams_short_clips_PARTIAL_TRANSCRIPT.md`.

## WILLIAMS-1 rules
| # | Rule | Source | Status |
|---|---|---|---|
| D1 | Trend is the basis of all profit; trend needs time → day traders must pick their days | NT ch.2, CL | STATED |
| D2 | Range cycle: small ranges → large ranges; small open-to-close → next day large range ("Blastoff") | NT ch.4, TG | STATED; threshold/formula **MISSING** (NinjaTrader-only download) |
| D3 | Large-range days close at their high/low → hold to the close | NT ch.4, CL | STATED |
| D4 | Close-open pattern (S&P RTH, buy open / exit close): down close + higher open best; down close + lower open profitable; after up close both lose | NT ch.7-8 | STATED (S&P stats) |
| D5 | Day of week: S&P long bias Mon/Tue/Thu, weak Wed; bonds down Wed; gold Thu/Fri | NT ch.5 | STATED |
| D6 | Combine D4 × day of week (down close + lower open weak on Thursday, best Friday) | NT ch.8 | STATED |
| D7 | Intraday volatility U-shaped: trade after the open and before the close | CL | STATED |
| D8 | Close location within range: low close → more bullish next day (and vice versa) | TG | STATED; as a rule → INTERPRETED |
| D9 | Volatility breakout from the open (bracket the open, trade the break); "Oops" (open below prior low, rally back above it) — both weaker since electronic markets; Oops still on Sunday-night session | TL ch.6-7 | STATED; breakout distance **MISSING** |
| D10 | Cycle forecast (3 dominant cycles, Timing Solutions software) as timing bias | NT ch.10 | **PROPRIETARY** |

Verdict: **Partial.** D3-D7 are directly testable on our NQ data (S&P stats → NQ must be re-tested). D2 needs a defined
threshold (interpreted, e.g. |O-C| / range in the lowest N% of the last 20 days); D9 distances missing; D10 not copyable.

## WILLIAMS-2 (swing engine)
Weekly setup: COT commercials accumulating (and who drives open interest), seasonal pattern, valuation model, accumulation index,
cycles → list of set-up markets every Saturday. Daily trigger: trend change (break of the 1-3 day high, higher low, trendline break,
Oops). Stop at a chart point, size from it (4-5% risk, max 10%), target (Seamans-style) or his trend-following trailing line;
avg hold 12-15 days; comparative strength within a group (buy strongest, sell weakest). Never average down.
Proprietary: valuation, accumulation index, cycle forecast, trend line. → **Not copyable as a whole.**
Simple seasonals (Copyable): Walmart buy ~2 days before Thanksgiving, sell 3-4 days later; first trading days of November strong;
13-week earnings cycle; 4-year cycle.

## First 12.5-month test (2026-11-16)
See `../audits/PROS_batch_results_2026-11-16.md` (defaults approved by Kian; code `scalping/orderflow/pros.py`).
