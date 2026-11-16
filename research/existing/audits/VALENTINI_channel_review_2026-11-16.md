# VALENTINI · review of his full YouTube channel + IQ Capital interview (2026-11-16)

Sources read in full (all Gemini transcripts, not checked against captions), saved in `sources/`:
| Short | File | Notes |
|---|---|---|
| CH | `valentini_yt_challenge_interview_TRANSCRIPT.md` | IQ Capital interview with Brandon (~60 min). Its live part = our VALENTINI-6 source |
| OFG | `valentini_yt_orderflow_guide_TRANSCRIPT.md` | "The Only Orderflow Guide": definitions + profile framing for the daily bias |
| 3T | `valentini_yt_3_tricks_TRANSCRIPT.md` | 19 Sep 2025 example (in our data) |
| TOP3 | `valentini_yt_top3_competition_TRANSCRIPT.md` | World Cup session, 30 Dec |
| EZ | `valentini_yt_entry_zones_TRANSCRIPT.md` | Delta Print, volume ledge |
| LIQ | `valentini_yt_liquidity_guide_TRANSCRIPT.md` | Heatmap / order book; live 4 May 2026 (in our data) |
| M10K | `valentini_yt_minus10k_session_TRANSCRIPT.md` | Worst session; "the 23rd", 24,932 (23 Sep 2025 or 23 Feb 2026 in our data) |
| 20R | `valentini_yt_20R_session_TRANSCRIPT.md` | December session, 25,650-25,534 (several Dec 2025 days possible) |
| BEST | `valentini_yt_best_session_TRANSCRIPT.md` | Best session of October |
| RISK | `valentini_yt_risk_protocol_TRANSCRIPT.md` | Risk protocols per account type |
| 5E | `valentini_yt_5_edges_TRANSCRIPT.md` | IVB statistics + 4 long-term edges |
| ZERO | `valentini_yt_start_from_zero_TRANSCRIPT.md` | AMT regimes, transition candle, position building, candle framing |
| IVB | `valentini_ivb_TRANSCRIPT.md` | "The Simplest Orderflow Trading Model" (already in VALENTINI-7) |
Times in brackets are the transcript's own timestamps or section headers.

---------------------------------------------------------------------------------------------------------------------
## 1. Answers to our open questions

### VALENTINI-7 IVB (all five questions)
| Q | Answer from his words | Source | Consequence |
|---|---|---|---|
| R1 range | **30 min**: "the high and low price established during the first 30 minutes of a regular cash trading [session]"; "the battle of the first 30 minutes" | 5E 8:10; RISK 30:30 | 30 min main confirmed; 15 min variant can go |
| X target / R:R | "I have one quant system, the IVB, it's **negative risk-to-reward**"; "IVB is **1.44** [profit factor] over five years of data"; base edge "skew of 13.5% of being right" at 1:1 | CH ~32:00 (validation section); 5E 8:53-9:03 | **A9: our fixed 2.5R target contradicts him.** His IVB quant targets the protection level (65-70% hit) with target <= risk. Fixed 2.5R explains part of the -0.13R |
| Breakout candle | "What came to my mind is a strong candle is bringing you a bigger expansion. No, **a small candle with a lot of delta** is bringing you a bigger expansion, because if it's a big candle with a lot of delta, usually the market... likes to rebalance" | CH (section "Edge decay", RSI/IVB answer) | **B8: breakout filter** = small range + large delta on the breakout candle. Codable |
| Absorption as trigger | "when there is an absorption, the market is probably to give you a continuation... **don't use it to time the bottom, but use it to put a stop order below [beyond], wait for the breakout, and accelerate**" | CH (same answer) | **A10: absorption entries = stop order beyond the absorption, not a bar-close entry.** Our V7 absorption entries (bar close) were the losing part (-0.27R) |
| Stop | aggressive: "cover myself below the area where the buyers got aggressive"; conservative: "point A to point B with a profile... **cover my stop loss below** [the] bulk of delta protection of the buyers... I enter at the test of the big trade" | ZERO 9:05-9:25 | Two stated stop variants; replaces my default |
| Short side | "you can also implement the short side, that is more strong, but you should be able to analyze the **gamma regime**" | 5E 9:14 | Short side needs options data (MISSING) → long-only main, short as variant |
| Bias use | IVB = intraday bias: "the first time where they show me their intention is this candle"; "you are not using discretionality to frame your bias" | ZERO 8:44; IVB 14:44 | Confirms IVB as a mechanical day bias |
| Audit | validated in Python/MultiCharts (Luca), audited by ex-market maker Matteo Conti | 5E 7:36; RISK 26:55 | External validation exists for the base edge |

### Day bias / "momentum day" (VALENTINI-3 A6, suite A4)
| Point | His words | Source | Status |
|---|---|---|---|
| Profile for the daily bias | "I only use the **cash session profile**... I find it from statistical validation the London session to be usually for US indices not so valuable to add to the profile" | OFG 28:43-29:11 | STATED. **CONFLICT** with CF 1:42:47 (live: "profile on everything they built also during London") and with our suite's full-day profile (A4) → test both |
| Reading it | value accepted above previous VA + POC higher (> 50% of range = P-shape) → long bias; rejection area below VAL = support; overlapping days → merge profiles; two days rejecting the top + lower POC/VA = warning / distribution; breakout of merged VAL = trend | OFG 29:53-36:56; ZERO 13:24-18:29 | STATED, mechanical enough to code (thresholds INTERPRETED) |
| Transition | "we start to see big orders accepting value outside... the transition candle or initiative auction"; fundamental catalyst usually behind it | ZERO 3:51, 7:39 | STATED |
| Bias stack | composite/weekly → daily (cash profile) → IVB → 80-range candle framing → 40-range execution | ZERO 20:52-24:54; WoR 5:32 | STATED (top-down order) |
| AAA | only "use part of this profit to input additional risk in the next sample, maybe the **AAA sample**" | RISK 26:55 | AAA rules still not in any source. A6 can now be replaced by the stated **profile-framing bias** (proposal) |

### Other open points
| Point | Answer | Source |
|---|---|---|
| Execution chart | NY open on **40-range bars** (~5 min): "if you do one-minute candle, you can have huge candle and all the data inside gets aggregated"; NASDAQ Effort "works" on 40-range; candle framing on 80-range | WoR 5:32, 84; ZERO 21:49, 24:30 | CONFLICT with CF (5m/1m). Our V1/V4 run on 1m → variant: 40-tick range bars |
| VALENTINI-6 B8 (Order Track timeframe) | 40-range | ZERO 24:30 | partly answered; Effort stays proprietary |
| Big-order filter | "Today there is a lot of volume, so I need a **bigger filter**"; DeepCharts bubble grouping setting | M10K 0:00; LIQ 23:41 | filter is adapted to the day's volume → INTERPRETED (e.g. scale 30 by volume) |
| How long a big order matters | big single orders influence price "**5, 10, and 15 minutes later**" (market maker study, matches his experience) | CH (why-edges-exist answer) | gives a natural time horizon / time stop |
| VALENTINI-5 Model B | not mentioned in any video | – | P5 stays MISSING |

---------------------------------------------------------------------------------------------------------------------
## 2. Precise definitions (replace my INTERPRETED counts)
| Pattern | His definition | Source |
|---|---|---|
| Absorption | high effort, **no result**: "huge effort on the top from the buyers... completely absorbed... candle closes sell... **positive delta, but negative closure — the textbook example**"; or delta strongly one way, candle closes the other way | OFG 11:33-12:15, 14:42-15:19 |
| Aggression / initiative auction | high effort **and** result: strong delta + full candle in direction + volume above average; "the delta is leading the price" | OFG 12:23-13:10, 15:42-16:46 |
| Book sweeping | low effort, high result (thin book) | OFG 13:19 |
| Exhaustion | price pushing on **decreasing volume** + contrarian imbalance at the extreme → snap back to value | OFG 18:26-19:20 |
| CVD divergence | "lower low on the cumulative volume delta while the market is still on the same level" = absorption → failed auction | 3T 5:22; TOP3 1:58 (CVD leading a breakdown) |
| Fair value gap | lowest-volume level inside the VA of a fixed profile from swing A to B (a single level, not a zone) | 3T 3:21 |
| Delta Print / volume ledge | level with the most delta pressure in the previous swing (+ big trades, + VA edge confluence); ledge = sharp HVN→LVN edge | EZ 1:32-2:27, 4:29-4:51 |
Consequence: absorption/aggression can be coded from bar delta vs close (his definition) instead of "≥ 2 big orders" (mine).

---------------------------------------------------------------------------------------------------------------------
## 3. Management (input for the management-module audit; not coded yet)
| # | Rule | Source | Note |
|---|---|---|---|
| G1 | **No partial profits**, trail all the way: "I tested the partial profit... on NASDAQ... partial profit can take you out here and then you have 200 points of rally"; Brandon confirms his algo tests | CH (trailing section, end) | **CONFLICT** with CF ("half off at weakness", 2:21) and WoR ("took 5,000 out"). Latest + tested statement → main |
| G2 | Trail behind the **last aggression / last point of break**; tighter when the other side shows big orders | CH live part; OFG 40:26; 20R 2:15, 6:45; BEST 3:34 | STATED |
| G3 | Risk to zero when the level fails or the other side takes control ("when I see that the sellers take control, I put my risk to zero") | CH live part; BEST 1:42 | STATED |
| G4 | **Position building**: move all stops to the last invalidation (locks the floating profit), then risk **one third of the locked profit** on an additional trend position | ZERO 11:28-12:31 | STATED, codable |
| G5 | After early gains, only the day's profit is at risk ("I'm only risking the profit of the day") | BEST 2:23; TOP3 3:33 | STATED (= our house-money rule) |
| G6 | **Session stop at break-even**: "maximum drawdown that is break-even — so if we go back to break-even, we stop the session" | BEST 1:01 | STATED, codable |
| G7 | Daily loss budget, platform blocks trading at the limit | CH 0:00 section | STATED (= our 2% / 3 losses) |
| G8 | Personal account 0.25-0.5% per trade | RISK 30:30 | confirms our 0.25% |
| G9 | Best/worst session ratio ~2.5:1 ($25k vs $5-10k) is his benchmark | BEST 3:00-3:22 | KPI idea |
| G10 | Bigger targets only in volatility; "if you want a smoother profit, take profit more often in consolidation" | 20R 2:15 | regime-dependent target |

---------------------------------------------------------------------------------------------------------------------
## 4. Validation and edge-decay rules (for our research loop / validator)
| # | Rule | Source |
|---|---|---|
| V-a | Test triggers in isolation at **fixed 1:1** to see which trigger is most efficient, then align it with the bias | CH (data-analysis answer) |
| V-b | **Edge contribution**: add one component at a time (absorption, aggression, exhaustion) and keep it only if signal quality improves | CH 32:28 section |
| V-c | Real edge = **plateau** of parameters, not a peak (300% works, 301% fails = overfit) | CH 32:28 section |
| V-d | PF **1.4-1.6 over thousands of trades** is excellent; PF 4-6 sustained = HFT or overfitting; always include commission + slippage | CH (validation answer) |
| V-e | **Rolling quarter** check: ~200 executions per quarter; equity first goes sideways; if a quarter flips down vs the previous one → stop the model | CH 10:15 section |
| V-f | 3 years of in-sample + 3 months out-of-sample (~300 trades) before using a model | CH live part (end) |
| V-g | A model profitable on NQ can lose on ES ("microstructure is not the same") | CH live part (end) |
| V-h | Diversify across models; trend models suffer in consolidation, accept it | CH; 5E 2:00; ZERO 6:55 |

**Important for reading our results:** he says his **pure trend-following momentum model underperformed in 2025** after
Trump ("explosion back inside the range"), he kept trading it too long and lost "probably six figures", and switched
(value migration model, IVB). Our data (Jun 2025 - Jun 2026) is exactly that regime → V1/V4 results are tested in
their worst regime, by his own account.

---------------------------------------------------------------------------------------------------------------------
## 5. New models found (candidates, each needs its own rule sheet + Kian's approval)
| # | Model | Core | Copyable? |
|---|---|---|---|
| N1 | **Value-migration 1:1 scalper** ("Order Track", his newest, 6 months old) | per candle (40-range): candle POC/VA shifts in the bias direction + delta agrees → enter at the candle's value area, stop beyond it, 1:1 target; repeat while value keeps migrating; filter with NASDAQ Effort | base codable from trades; Effort layer proprietary (= VALENTINI-6). He says he would not automate it because of management |
| N2 | **Profile-framing daily bias** | RTH profile acceptance/rejection, P-shapes, merged profiles (section 1) | codable; a bias filter for V1/V3/V4/V7 |
| N3 | **Balance-phase rotations** | in balance: rejection at a wall + initiative candle → trade to the other wall, 1:1.5-1:2 | codable (needs balance detection) |
| N4 | **Candle framing / change of flow** | on 80-range: compression won by one side, POC migrates and value is accepted → 1:2 | codable |
| N5 | **Delta Print location** | strongest-delta level of the last swing as entry zone (our old B4) | now testable with real delta |
| N6 | Liquidity heatmap patterns (reload, iceberg, grill, book flip) | resting limit orders | **not copyable**: needs order-book data (MBP-10/MBO), we only bought trades |
| N7 | Long-term edges: PEAD, politician tracking, volatility risk premium, MVRV Bitcoin DCA | academic edges | outside the scalping engine; possible separate APEX tracks |

---------------------------------------------------------------------------------------------------------------------
## 6. Mindset / process statements worth keeping
- "Data analysis and proper risk protection" are what he would teach first; ego (moving stops, refusing to stop a model) is the main failure (CH 0:00).
- Every strategy has regimes where it loses; protect capital there; consolidation weeks are a cost of business (CH; ZERO).
- Don't trust gurus (including him): research, validate in simulation, then trade (CH; 5E 3:10).
- Competition / prop / personal accounts need different risk geometry; personal = risk-adjusted, moderate (RISK).
- Diversify brokers too (FTX lesson) (RISK 26:55).

---------------------------------------------------------------------------------------------------------------------
## 7. Days in our Databento data that his videos show (future fidelity checks)
19 Sep 2025 (3T) · 23 Sep 2025 or 23 Feb 2026 (M10K, level 24,932) · Dec 2025 (20R, several candidate days) · 4 May 2026 (LIQ live) ·
15/16 Oct 2025 (WoR AAA day, see VALENTINI-3) · 23 Jun 2025 (masterclass, done).

## 8. Proposed changes (await Kian, nothing changed in code)
1. IVB: target ≤ 1R (his "negative risk-to-reward") instead of 2.5R; absorption entries as stop orders beyond the absorption;
   breakout filter "small candle + big delta"; long-only main.
2. Absorption/aggression detection by his effort-vs-result definition (delta vs close) as a variant of the big-order count.
3. Daily bias variant: RTH profile framing (N2) for V1/V3/V4/V7.
4. Execution-chart variant: 40-tick range bars for the NY models.
5. Management module audit using G1-G10 (no partials, trail behind last aggression, position building, break-even session stop).
6. Validation rules V-a..V-h into `research/CYCLE.md` (rolling-quarter monitor = edge-decay switch).
