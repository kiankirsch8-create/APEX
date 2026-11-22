# Momentum scanner (Ross Cameron's 5 pillars)

A self-built "Day Trade Dash"-style scanner for small-cap momentum, on IBKR's server-side scanners plus free
float and news sources. Read-only: it never places orders.

| Pillar | How it's measured | Source |
|---|---|---|
| 5x relative volume | volume so far today vs the volume a normal day has by this time (`RVol`); `Volume×day` = vs a full average day | IBKR live quote (avg = 90-day) |
| Up 10%+ | last price vs previous close, pre-market included | IBKR |
| Catalyst | headline in the last 24 h | IBKR news (free providers), GlobeNewswire, PR Newswire, Business Wire, ACCESSWIRE, SEC 8-K |
| $1-20 | scanner filter + check | IBKR |
| Float < 10M (amber 10-20M) | daily cache | Finviz, fallback Yahoo |

## Setup
1. IBKR account with **US stock market data (non-professional)**. Start IB Gateway or TWS, enable the API
   (Settings → API → "Enable ActiveX and Socket Clients"), port 4002 (Gateway paper) or set `IB_PORT`.
2. `pip install ib_async`
3. Optional: `set SEC_USER_AGENT="Your Name you@example.com"` (SEC asks automated clients to identify themselves).
4. `python -m scalping.scanner.run` and open http://127.0.0.1:8765 . Click "Enable sound" once for chimes.

Without IBKR: `python -m scalping.scanner.run --demo` shows the dashboard with simulated stocks.

## To verify on the first live morning
- IB scanners return movers in pre-market (from 4:00 New York) — expected, not yet tested.
- Float coverage: share of candidates where Finviz/Yahoo returned a float.
- News lag: time between a press release and its label in the scanner.
- Relative volume vs another source (e.g. the Dash trial or TradingView's RVOL) on the same stock.

## Replay a past morning (works on weekends)
`python3 -m scalping.scanner.replay 2024-08-21 VRAX GOVX BPTH GDC MNPR APDN` (add `--speed 120`, `--start 07:00`).
Uses IBKR historical 1-minute bars incl. pre-market, previous daily bars and IB news. IBKR has no historical scanner,
so you name the tickers. Float is today's value. Delisted tickers are skipped.

## Order-flow window (Bookmap-style)
`python3 -m scalping.scanner.book GDC` (or `NQ`, or `--demo`) → http://127.0.0.1:8766 . Clicking a ticker in the scanner
switches it. Shows liquidity heatmap, volume bubbles, best bid/ask, current order book (COB) and volume profile (CVP).
Depth = the Level 2 feeds subscribed at IBKR, merged (free IEX only = thin; Nasdaq TotalView = full for Nasdaq stocks).
Bubbles and bid/ask use Level 1 data and are complete already.

Tests: `python -m pytest scalping/scanner/test_scanner.py -q`
