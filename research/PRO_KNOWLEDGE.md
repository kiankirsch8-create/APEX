# PRO KNOWLEDGE · what the world's best traders say that isn't a strategy (for the research agents)

**What this file is.** A learn-book for the APEX agents (researcher, validator, any future strategy builder). It collects
principles, market facts, filters and validation habits that pro traders state in their interviews and courses, but that
are **not complete strategies** on their own. Use them to design better hypotheses, filters, risk rules and tests.
Complete strategies live in `existing/rulesheets/`; Kian's personal lessons live in his Playbook doc (not here).

**How to use it.**
- Every item = one principle, who says it, where (file in `existing/sources/`), and **how an agent can use it**.
- These are **claims by successful traders, not proven facts.** Treat each as a hypothesis to test on our data, never as a
  rule to hard-code without a test. Where our own tests already said something, it is marked **[our test]**.
- When you build an idea on an item, cite its ID (e.g. `PK-R3`) in the ledger mechanism, so we can see which pro knowledge helps.
- Status: STATED = said clearly; PARTLY = said, but vague or with conflicts; THIRD-PARTY = from a documentary/other person.
- Traders (codes): VAL Fabio Valentini · NIL Patrick Nill · CAM Ross Cameron · CRE Chris Creamer · WIL Larry Williams ·
  TRU David Trullas · COH Steve Cohen.

Last update: 2026-11-16 (7 traders). Add new items at the end of each section; never delete, mark outdated instead.

---------------------------------------------------------------------------------------------------------------------
## 1. Regimes and adapting to changing markets (PK-R)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-R1 | **"If you're not changing, you're dying."** Edges get copied; excess returns get competed away; he kept changing the firm even in good years | COH `cohen_fuhrman_investing_art`, `cohen_point72_career_part1` | Expect every edge to decay. Build an edge-decay monitor (see PK-V5) and keep a pipeline of new ideas instead of tuning one forever |
| PK-R2 | Markets in the 1970s-90s were inefficient and easy; today the easy edges are gone and **liquidity is lower than it used to be**, which forces new methods | COH `cohen_fuhrman_investing_art` | Old textbook setups (simple breakouts, simple data) start from a prior of "probably arbitraged". Old-paper edges need a recent-period test, not just a full-history test |
| PK-R3 | **Strategies are regime-dependent.** Continuation models die in chop; reversal models get run over in trends unless at an extreme location | CRE `creamer_orderflow_course_condensed`, `creamer_AR_model_course_PART1` | Always tag each trade with a regime label (trend / balance / compression) and report results per regime. A model that only works in one regime needs a regime filter, not a different stop |
| PK-R4 | Edge = **environment + location + participation + execution**; "signals do not create the edge, the filters before them do" | CRE `creamer_AR_model_course_PART1` | Spend research effort on the context filter (where/when to trade), not only on the trigger |
| PK-R5 | Three environments: initiative expansion (continuation), balance rotation (fade the extremes), compression (**no trade**) | CRE `creamer_AR_model_course_PART1`; VAL "the cage" `valentini_iqcapital` | Add a "no-trade" class. Most of the gain from a filter often comes from removing compression days |
| PK-R6 | **No-trade checklist**: 2-3 of repeated failed breakouts, overlapping structure, repeated return to value, no higher-timeframe alignment, no clean location, no continuation path → skip the day | CRE `creamer_AR_model_course_PART1` | Codable day filter; test it as an add-on to any intraday model |
| PK-R7 | Markets range ~70% of the time and trend 20-30% | NIL `nill_iqcapital_3touch`; VAL "70% of the time the market is stationary" `valentini_words_of_rizdom` | Prior for model design: a trend-only model sits out or loses most days. Check our own NQ split before relying on it |
| PK-R8 | His **pure trend model underperformed in 2025** after the Trump volatility (explosions back inside the range); he kept trading it too long, lost "six figures", then switched models | VAL `valentini_yt_challenge_interview` | Our Jun 2025 - Jun 2026 NQ data is a bad regime for trend models by his account. **[our test]** Jan-Mar 2026 was the weak period for most Valentini models. Judge trend models over several regimes |
| PK-R9 | Rules must be **adjusted per market**: DAX fakes ~10 points past a level, ES only 1-2; a model profitable on NQ can lose on ES ("microstructure is not the same") | NIL `nill_iqcapital_3touch`; VAL `valentini_yt_challenge_interview` | Never port thresholds between instruments untested. Scale buffers by the instrument's typical fake-out size / ATR |
| PK-R10 | Some patterns **got weaker once markets went electronic** (open-range volatility breakout, "Oops" gap reversal); Oops still works on the Sunday-night session | WIL `williams_traderlion_interview` | Test old patterns on recent data only first; check if they survive in thinner sessions where the crowd is smaller |
| PK-R11 | Big moves need a cause: **the transition candle / initiative auction usually has a fundamental catalyst** behind it | VAL `valentini_yt_start_from_zero` | Tag trades with news/event days; expect continuation edges to be stronger on catalyst days |
| PK-R12 | **Late-cycle macro** (tight labour, rising wages, Fed leaning against it) usually ends in a bear market, timing unknown; returns become "marginal" | COH `cohen_fuhrman_investing_art` | Swing engine: macro regime as a slow filter (risk-on / risk-off), never as a timing signal |
| PK-R13 | Summer and some months are compressed (end of March, May, June, August) → mean-reversion fits, trend struggles | VAL `valentini_chart_fanatics` | Month/seasonal regime feature; test model performance by month |

## 2. Time of day, calendar and sessions (PK-T)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-T1 | Intraday volatility is **U-shaped**: trade after the open and before the close, the middle is slow | WIL `williams_clips_from_chat` | Time-of-day filter; measure our NQ volume/volatility curve and allocate signals to the active parts |
| PK-T2 | Direction of the NY day usually becomes clear **15-30 min after the open**; don't trade the 10-20 min before the open | VAL `valentini_chart_fanatics` | Opening-window exclusion and a "bias after 30 min" feature |
| PK-T3 | After a **first-hour expansion, NY rebalances until Power Hour** (~13:30-15:00 NY) about 90% of the time (not on news/tweet days) | VAL `valentini_words_of_rizdom` | Block trend entries between the first-hour expansion and 13:30 NY; test the 90% claim on our data first |
| PK-T4 | Day traders "must pick their days": **small-range days are followed by large-range days**; large-range days close near their high/low → hold to the close | WIL `williams_ninjatrader_live`, `williams_titans_godfather` | Volatility-contraction feature (narrow range / small open-close) to choose trend days; time-exit at the close on those days |
| PK-T5 | Day-of-week and close-open patterns exist (S&P: Mon/Tue/Thu stronger, Wed weak; down close + higher open best) | WIL `williams_ninjatrader_live` | Cheap features for any model; re-test on NQ (his stats are S&P and old) |
| PK-T6 | Trullas trades only the US open and is done by ~16:00 CET; Nill trades European mornings to ~14:00-15:00 CET and says the US open "often changes the market"; Creamer has a hard stop 1.5 h after the NY open | TRU, NIL, CRE | Most of the best intraday traders concentrate on **one active window**. Prefer models with a narrow, explained window over all-day models |
| PK-T7 | Avoid trading **into high-impact news**; no breakout entries near important news | TRU, NIL `nill_iqcapital_3touch`, VAL (Trump tweets) | News blackout filter (calendar exists in APEX: `high_impact_events_data.py`) |
| PK-T8 | **Rollover week**: trade the contract with more volume; the rollover gap tends to fill | TRU `trullas_rollover` | Data hygiene for back-adjusted series + a testable gap-fill idea |
| PK-T9 | Seasonal calendar trades exist (first trading days of November strong; Walmart into Thanksgiving; 4-year cycle) | WIL `williams_clips_from_chat` | Swing-engine features; small, cheap tests |

## 3. Order flow, volume and price behaviour (PK-F)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-F1 | **Effort vs result**: absorption = high effort, no result (e.g. positive delta but the candle closes down); aggression = high effort and result (delta + full candle + above-average volume); book sweep = low effort, high result | VAL `valentini_yt_orderflow_guide` | Code absorption/aggression from bar delta vs close instead of arbitrary counts. **[our test]** in V7 the aggression trigger was +0.08R, the absorption trigger -0.27R (bar-close entries) |
| PK-F2 | **Absorption predicts continuation, not the turn**: "don't use it to time the bottom; put a stop order beyond it, wait for the breakout, and accelerate" | VAL `valentini_yt_challenge_interview` | Enter absorption setups with stop orders beyond the absorption, not limit/close entries |
| PK-F3 | A **small candle with a lot of delta** gives a bigger expansion than a big candle with a lot of delta (big candles tend to rebalance) | VAL `valentini_yt_challenge_interview` | Breakout-quality feature: delta per point of range on the breakout bar |
| PK-F4 | Big single orders influence price for about **5, 10 and 15 minutes** afterwards (market-maker study) | VAL `valentini_yt_challenge_interview` | Natural time horizon / time stop for big-trade triggers |
| PK-F5 | Big-order size filter must **scale with the day's volume** ("today there is a lot of volume, so I need a bigger filter") | VAL `valentini_yt_minus10k_session` | Make size thresholds relative (percentile of the day's trade sizes) rather than fixed |
| PK-F6 | **Volume divergence**: a new high/low on lower volume than the previous one (indicators not confirming) = the move is tiring | TRU `trullas_pasted_in_chat_NOTES` (D), `trullas_sweep_volume_divergence`; VAL exhaustion = pushing on decreasing volume `valentini_yt_orderflow_guide` | Exhaustion feature for reversal models; also a filter that blocks breakout entries on weak volume |
| PK-F7 | **Breakouts need volume**; a poke through a level without volume is not a breakout | TRU `trullas_clusters_alex_marquez`; CRE participation filter (no trades if MNQ 5-min volume < 20,000) `creamer_iqcapital_whiteboard` | Relative-volume condition on breakout bars and a minimum-participation filter per session |
| PK-F8 | **Levels are zones** (clusters of 5-20 NQ points), not single prices | TRU `trullas_clusters_alex_marquez`; CRE gamma levels as ~40-pt zones `creamer_gamma` | Model levels as zones scaled to volatility; avoid exact-price triggers |
| PK-F9 | Price moving fast through a zone with little volume (thin profile / big-body candle with little volume inside) tends to be **revisited later** | TRU "osmosis" `trullas_nvidia_osmosis`; VAL LVN logic `valentini_chart_fanatics` | Low-volume-node magnet feature for targets and pullback entries |
| PK-F10 | **Delta alone is unreliable**: he walks through day after day where the daily delta disagrees with the day's direction | TRU `trullas_delta_debate` | Never use delta as a stand-alone signal; use it with price location/close |
| PK-F11 | Each rejection at a level makes the breakout setup **weaker**; "every time I get more demanding" | VAL `valentini_chart_fanatics` | Count prior failed tests as a feature (more tests → require stronger confirmation) |
| PK-F12 | A range is "ready" for the breakout after about the **3rd touch** | NIL `nill_iqcapital_3touch` | Touch-count feature; note it conflicts in spirit with PK-F11 → test which holds on NQ |
| PK-F13 | Small range after a huge move → breakout trades; big range → range trades (breakout stop too far) | NIL `nill_iqcapital_3touch` | Choose the model by range size relative to the prior impulse |
| PK-F14 | Big resting limit orders in the book act as magnets/targets; heavy resting supply blocks longs | TRU `trullas_pasted_in_chat_NOTES` (A); VAL heatmap `valentini_yt_liquidity_guide` | Needs order-book data (MBP-10/MBO) we don't have → note as a future data option |
| PK-F15 | Options **gamma regime**: positive gamma dampens moves (fade the edges), negative gamma amplifies them (continuation) | CRE `creamer_gamma`, `creamer_iqcapital_whiteboard`; VAL short side needs gamma `valentini_yt_5_edges` | Strong candidate regime filter; needs QQQ/NDX options open-interest history (not bought) |

## 4. Entries, stops, targets and trade management (PK-M)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-M1 | Don't take the **first drive** out of balance; wait for the second | VAL `valentini_chart_fanatics` | Second-test entry variant for breakout models |
| PK-M2 | Stops go **beyond the participants who would be proven wrong** (the big order, the failed side), not at a fixed distance | VAL, CRE `creamer_iqcapital_whiteboard` | Structure-based stops; compare against fixed-ATR stops |
| PK-M3 | Near obvious highs/lows, put the stop **1-2 ticks inside** the level to beat the stop cascade | VAL `valentini_chart_fanatics` | Stop-placement tweak; measure slippage at obvious levels |
| PK-M4 | **Never widen a stop**; never average down a loser; add only to winners | NIL (oil -42 lesson), WIL, TRU, CRE `creamer_world_cup_competition` | Hard constraints in every engine |
| PK-M5 | First check after entry: price must **move away / re-enter value quickly**; if not → break-even or cut ("breakout or bailout") | CRE `creamer_iqcapital_whiteboard`; CAM `cameron_1min_scalping_training`; VAL "risk-free in 1 minute" | Time-stop / no-progress exit; test N bars to break-even vs exit |
| PK-M6 | **No partial profits on NQ** (tested by Valentini, hurts: partials cut the 200-point rally) | VAL `valentini_yt_challenge_interview`, `valentini_iqcapital` | Default to full exits on NQ trend trades; test partials only as a variant. **[our test]** his trailing management (as interpreted) cut V1/V4 winners |
| PK-M7 | Targets should match the regime: bigger targets only in volatility; in consolidation take profit more often | VAL `valentini_yt_20R_session` | Volatility-scaled targets |
| PK-M8 | Fibonacci zones used by several pros: entry ~23-25% retrace (TRU), 0.705-0.886 discount zone (CRE); targets 38.2/50/61.8-66% | TRU, CRE | Common, testable location grid; test which levels actually matter on NQ |
| PK-M9 | Take targets **1 tick before** the level; slippage on stop entries in squeezes is real | TRU `trullas_pasted_in_chat_NOTES` (D); VAL `valentini_words_of_rizdom` | Fill-realism rules in the backtester |
| PK-M10 | Position building: lock profits by moving stops to the last invalidation, then risk a fraction (1/3) of the locked profit on an add | VAL `valentini_yt_start_from_zero` | Pyramiding module that never increases initial risk |
| PK-M11 | **Size by confluence**: more confirming signals → more contracts (Trullas 1-4 contracts) | TRU `trullas_pasted_in_chat_NOTES` (D) | Confidence-scaled sizing; compare to flat sizing |
| PK-M12 | **Size down when volatility is high** (5-min ATR 50-60 pts → 3-5 micros instead of 10) | CRE `creamer_feb_recap` | Volatility-targeted sizing |

## 5. Risk and daily process (PK-K)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-K1 | Pros risk small per trade in personal accounts: VAL 0.25-0.5%, NIL 0.1-0.3% (1% rule), CRE small; contest risk (2-30%) is not a model | VAL, NIL, WIL, TRU | Default 0.25% per trade; never copy contest risk |
| PK-K2 | **Daily stops**: VAL 2% / 3 losses, NIL 3-4 losses, CRE 2 losses in a row, TRU 2-3 "bullets" | all | Daily loss cap + consecutive-loss stop in every intraday engine |
| PK-K3 | **Stop after a big win** / once the day's target is reached (VAL after a big zero-retracement wave; TRU above ~$1,000 profit looks for the exit) | VAL, TRU | Test a daily profit-lock (e.g. stop after +X R or after giving back Y% of the day's peak) |
| PK-K4 | After early profit, **risk only the day's profit** (house money); session stop when back to break-even | VAL `valentini_yt_best_session` | Already in the suite; reuse in new engines |
| PK-K5 | Even the best are right only **53-55% of the time**; what matters is what you do when wrong | COH `cohen_point72_career_part1` | Don't filter for high win rate; optimise expectancy and loss control |
| PK-K6 | Cut losers fast and without emotion; "do you want to be right or make money?" | COH `cohen_point72_career_part1`, `cohen_doc_most_profitable_daytrader` (THIRD-PARTY) | Hard stops and model kill-switches; no "hope" logic (no stop widening, no waiting for a comeback) |
| PK-K7 | Cohen cut his whole Mets roster when the playoff odds were ~15%: **cut positions whose expected value has collapsed**, even big ones | COH `cohen_cnbc_squawk_2024` | Re-evaluate open trades/models on current odds, not on sunk cost |
| PK-K8 | **Decentralised risk**: many independent PMs, each with risk limits set from the top | COH `cohen_cnbc_squawk_2024` | Portfolio of independent models, each with its own risk budget and kill-switch |
| PK-K9 | Diversify brokers (FTX lesson) | VAL `valentini_yt_risk_protocol` | Ops: plan at least two execution venues before going live |

## 6. Research and validation habits (PK-V)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-V1 | Real edge = a **parameter plateau**, not a peak ("300% works, 301% fails = overfit") | VAL `valentini_yt_challenge_interview` | Robustness check: neighbouring parameters must also be positive |
| PK-V2 | PF **1.4-1.6 over thousands of trades** is excellent; PF 4-6 sustained = HFT or overfitting; always include commissions + slippage | VAL; NIL wants PF ≈ 2 and DD < 10% (reject > 20%) | Sanity bounds for validator results |
| PK-V3 | Test triggers alone at a fixed 1:1 first, then add one component at a time and keep it only if signal quality improves | VAL `valentini_yt_challenge_interview` | Ablation as a standard step in every research run |
| PK-V4 | 3 years in-sample + 3 months out-of-sample (~300 trades) before using a model; NIL wants consistency over 5 years | VAL, NIL | Minimum sample rules for the validator |
| PK-V5 | **Rolling-quarter monitor**: ~200 trades/quarter; if a quarter turns down vs the previous one → stop the model | VAL `valentini_yt_challenge_interview` | Edge-decay kill-switch for live models |
| PK-V6 | Use data so you don't have **selective memory** (you remember the wins, forget the losses); know what you do well and what not | COH `cohen_point72_career_part2` | Per-setup, per-regime scorecards for every model, live and backtest |
| PK-V7 | **"Throw your best pitch"**: a pitcher with a great fastball shouldn't throw the weak changeup 45% of the time | COH `cohen_point72_career_part1` | Allocate risk to the setups with proven edge; cut or shrink the weak sub-setups instead of keeping them for variety |
| PK-V8 | Easy data is commoditised; you must **"interrogate the data"** (e.g. a regional product test → national rollout) to get an edge | COH `cohen_fuhrman_investing_art` | Prefer features that need work (custom order-flow measures) over standard indicators everyone has |
| PK-V9 | Don't trust gurus (including him): research, validate in simulation, then trade; parts of famous methods are often withheld | VAL `valentini_yt_challenge_interview`; TRU (Helite registered); NIL (zone rules secret) | Missing rules are normal; label them INTERPRETED and test variants rather than guessing one |
| PK-V10 | A public, simple version of a famous trader's method (Trullas's SMA 6/70/200 cross) backtested poorly (few trades, mixed) | TRU via third-party backtest `trullas_pasted_in_chat_NOTES` (E) | Don't assume a guru's simple public rule is the edge; the edge is usually in the context/filter they don't share |
| PK-V11 | Pros would **not automate their discretionary management** (Valentini: management needs discretion) | VAL `valentini_iqcapital`, `valentini_chart_fanatics` | Expect copied management rules to be the weakest part; test simple mechanical exits against them |
| PK-V12 | Competitive edge comes from new tools: Cohen rebuilds toward **quant/AI**; one LLM project saved his firm ~$25M; quantum computing "coming down the pike" | COH `cohen_cnbc_squawk_2024`, `cohen_fuhrman_investing_art` | Keep investing in research automation (this agent system) and new data sources |

## 7. Market and macro context (PK-C) — mostly for the swing engine
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-C1 | **Trend is the basis of all profit**, and trend needs time; "ride the horse in the direction it's going" | WIL `williams_ninjatrader_live`, `williams_titans_godfather` | Trend filter as default for swing ideas |
| PK-C2 | COT commercials accumulating + seasonal + valuation → set-up markets weekly, then a daily trend-change trigger | WIL (see WILLIAMS-2) | Swing-engine framework |
| PK-C3 | Crowd sentiment is contrarian at extremes: bad news on TV near bottoms, euphoric headlines near tops | TRU `trullas_divergence_fib_qna` (euphoria warning); a commentator in the Cohen documentary: great traders "smell the fear" (THIRD-PARTY `cohen_doc_most_profitable_daytrader`) | Sentiment/news-tone feature at extremes (APEX already has news/intelligence modules) |
| PK-C4 | **AI is a durable theme**, not a bubble yet (2024 view); big winners and losers; "reminds me of the 90s, not 1999" | COH `cohen_cnbc_squawk_2024` | Sector/thematic context for swing; dated view → re-check |
| PK-C5 | Long/short books should be judged on risk-adjusted return, not against the index in bull markets | COH `cohen_fuhrman_investing_art` | Report Sharpe/DD alongside raw return vs buy-and-hold |
| PK-C6 | Index vs active: flows keep moving to index funds; liquidity for active traders is lower | COH `cohen_fuhrman_investing_art` | Expect thinner books outside peak hours; model slippage accordingly |

---------------------------------------------------------------------------------------------------------------------
## 8. Ross Cameron additions (small caps, but most transfer) (PK-X)
| ID | Principle | Who / source | How an agent can use it |
|---|---|---|---|
| PK-X1 | **Let your own statistics define the strategy**: he reads his edge from his trade analytics (by price range, relative volume, time of day, move size) and only trades where the data shows profit | CAM `cameron_scalping_class_zapp` | Every model gets a breakdown report by context bucket; drop buckets that lose (with enough trades), then re-validate out of sample |
| PK-X2 | You capture at best **half of a move** (spread, late exit) → only take trades whose expected move is ≥ 2× the target | CAM `cameron_scalping_class_zapp` | Entry filter: expected range (ATR/realised move) ≥ 2 × target + costs |
| PK-X3 | High accuracy with avg win ≈ avg loss (68.5%, 1:1) is a valid profile; the danger for scalpers is an **inverted win/loss ratio** from slow loss-cutting | CAM | Monitor avg loss / avg win; alert when it drifts above ~1.2 |
| PK-X4 | Pullback quality: ≤ 2 red candles, red volume lighter than the green before it, retrace ≤ 50% (best in the top 25%) | CAM `cameron_micro_pullback_candles` | Generic pullback-quality score usable on NQ continuation entries |
| PK-X5 | Price "respects" **round numbers** (half/whole dollars in stocks): orders cluster there, price stair-steps between them | CAM `cameron_micro_pullback_candles`; VAL round-number exits | Round-number features on NQ (100/50-pt levels) for targets and breakout entries |
| PK-X6 | **Multi-timeframe alignment** (10s, 1m, 5m all agree) before adding | CAM `cameron_micro_pullback_candles` | Alignment filter for adds/pyramiding |
| PK-X7 | **Front side vs back side**: trade only while above the 20 EMA with MACD open; after the cross, stop and wait | CAM `cameron_scalping_class_zapp` | Simple trend-state switch for any momentum model |
| PK-X8 | **Intermittent reinforcement**: a broken-rule trade that wins is the worst outcome, because it trains bad behaviour; rely on long-run data, not single outcomes | CAM `cameron_scalping_class_zapp` | Validator: never keep a rule change because of a few recent wins; require the sample rules (PK-V4) |
| PK-X9 | **Lean in on what's working, abandon what isn't** (same day, same instrument) | CAM `cameron_scalping_class_zapp` | Test an intraday "model heat" rule: size up a model after early wins in the session, stop it after early losses |
| PK-X10 | Start small, size up only after a cushion (first trade capped until the day is up) | CAM `cameron_dip_and_rip_recap`, `cameron_2017_midday_recap` | Intraday risk ramp; compare with flat risk |
| PK-X11 | Market "temperature" gauge: number of circuit-breaker halts / gappers today | CAM `cameron_dip_and_rip_recap` | Breadth-of-momentum regime feature (stock engine; for NQ: count of large-range bars) |
| PK-X12 | HFT algos read headlines for keywords and pull bids/offers in spikes, which creates slippage and amplifies moves | CAM `cameron_scalping_class_zapp`, `cameron_micro_pullback_candles` | Model extra slippage on breakout/stop entries; expect overshoots on news |

---------------------------------------------------------------------------------------------------------------------
## Change log
- 2026-11-16: created from VAL, NIL, CAM, CRE, WIL, TRU sources + COH (5 transcripts added today).
- 2026-11-16: section 8 from three more Cameron transcripts.
