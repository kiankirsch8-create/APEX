# APEX — Autonomous AI Stock Discovery Engine

APEX is **not** a lookup tool. It is a fully autonomous engine that scans the
market every morning, finds the highest-conviction opportunities, runs a deep
Claude-powered investment thesis on each one, and exposes the results through
a FastAPI backend ready to be consumed by a Lovable (or any) frontend.

The user **never** picks the stock — APEX does.

---

## Architecture

```
┌────────────┐     ┌────────────┐     ┌──────────┐     ┌────────────┐
│ screener.py│ →   │ analyzer.py│ →   │ scorer.py│ →   │ results/   │
│  S&P 500   │     │  Claude    │     │  upside, │     │  daily_*.  │
│  + Nasdaq  │     │  Opus 4    │     │  prob,   │     │  json      │
│  100 scan  │     │  thesis    │     │  reason  │     │  latest.   │
└────────────┘     └────────────┘     └──────────┘     │  json      │
                                                       └────────────┘
                                                              │
                                                              ▼
                                                       ┌────────────┐
                                                       │  api.py    │
                                                       │  FastAPI   │
                                                       │  /api/...  │
                                                       └────────────┘

Driver: scheduler.py runs the whole pipeline every day at 07:00 local time.
```

| File              | Purpose                                                         |
|-------------------|-----------------------------------------------------------------|
| `screener.py`     | Scans the universe, applies bullish/bearish signal rules.       |
| `analyzer.py`     | Builds a full data packet, calls Claude, parses JSON thesis.    |
| `scorer.py`       | Computes display upside %, probability %, brief reasoning.      |
| `scheduler.py`    | Runs the pipeline daily at 07:00 (or `--run-now`).              |
| `api.py`          | FastAPI server on `0.0.0.0` for the frontend.                   |
| `master_prompt.py`| The APEX analyst system prompt (verbatim).                      |

---

## Quickstart

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API keys

```bash
cp .env.example .env
# then edit .env and fill in:
#   ANTHROPIC_API_KEY (https://console.anthropic.com/)
#   POLYGON_API_KEY   (https://polygon.io/)
#   NEWSAPI_KEY       (https://newsapi.org/)
```

### 3. Run the pipeline once (manual trigger)

```bash
python scheduler.py --run-now
```

This will:

1. Scan the S&P 500 ∪ Nasdaq 100 universe via Polygon.
2. Pick the top 5 highest-conviction candidates.
3. Build a full data packet (technicals + news + analysts + fundamentals + macro)
   and ask Claude (`claude-opus-4-20250514`) for a structured APEX thesis.
4. Score each thesis (upside %, probability %, one-sentence reasoning).
5. Write the results to `results/daily_picks_YYYY-MM-DD.json` and
   `results/latest.json`.
6. Append the run log to `logs/apex.log`.

### 4. Run the daily 7AM scheduler

```bash
python scheduler.py
```

This blocks forever and triggers the pipeline at 07:00 local time every day.
Override the time with `--at 06:30` or the `APEX_RUN_AT` env var.

### 5. Run the API for your frontend

```bash
python api.py
# or
uvicorn api:app --host 0.0.0.0 --port 8000
```

Server listens on `0.0.0.0:$PORT` (default `8000`) so a hosted Lovable frontend
can reach it. CORS is wide open.

---

## API Reference

| Method | Endpoint                  | Description                                                        |
|--------|---------------------------|--------------------------------------------------------------------|
| GET    | `/api/latest`             | Today's top 5 picks (`{date, generated_at, picks: [...]}`).        |
| GET    | `/api/pick/{ticker}`      | Full APEX report for one ticker from today's results.              |
| GET    | `/api/history`            | All dates with stored results: `{dates: ["YYYY-MM-DD", ...]}`.     |
| GET    | `/api/history/{date}`     | Picks for a past date (`YYYY-MM-DD`).                              |
| POST   | `/api/analyze/{ticker}`   | Manually trigger a fresh APEX analysis for any ticker.             |
| POST   | `/api/run-now`            | Manually run the full daily pipeline (bonus).                      |
| GET    | `/api/status`             | Whether today's analysis has run, when, and how many picks.        |

### Sample `/api/latest` response shape

```json
{
  "date": "2026-05-10",
  "generated_at": "2026-05-10T11:00:31+00:00",
  "count": 5,
  "picks": [
    {
      "ticker": "NVDA",
      "company_name": "NVIDIA Corp.",
      "current_price": 875.42,
      "apex_rating": "BUY",
      "upside_percentage": 34.5,
      "downside_percentage": -18.2,
      "probability_percentage": 78,
      "probability_reasoning": "Oversold RSI + analyst upgrade in a bullish macro environment",
      "target_30d": 920.0,
      "target_90d": 1020.0,
      "target_12m": 1180.0,
      "stop_loss": 715.0,
      "composite_score": 8.6,
      "confidence_level": "HIGH",
      "thesis": "...",
      "macro_signal": "BULLISH",
      "technical_score": 8.5,
      "fundamental_score": 9.1,
      "sentiment_score": 8.0,
      "analyst_score": 8.4,
      "historical_score": 7.5,
      "risks": [{"name": "...", "description": "...", "probability": "Medium", "impact_percentage": -15}],
      "historical_analog": "...",
      "catalysts": ["...", "..."],
      "verdict": "...",
      "direction": "UP",
      "generated_at": "2026-05-10T11:00:31+00:00"
    }
  ]
}
```

---

## Signals used by the screener

**Bullish**

- Volume ≥ 3× the 30-day average
- RSI between 28–38 (oversold but recovering)
- Within 3% of 52-week high (breakout imminent)
- Analyst upgrade or PT increase in last 48 h (from news headlines)
- Positive earnings surprise in last 7 days
- Insider buying in last 14 days
- 30-day drawdown of 15–40% with no fundamental break (overreaction)

**Bearish**

- RSI > 72 (extremely overbought)
- Insider selling > $5M in last 14 days
- Earnings/revenue miss + guidance cut in last 7 days
- Price extended ≥ 25% above 200-day moving average

Each signal carries a weight; the top 5 raw scores go to the analyzer.

---

## Scoring formula (scorer.py)

**Upside %**
- `direction == UP`: `((target_12m - current_price) / current_price) * 100`
- `direction == DOWN`: `((current_price - stop_loss) / current_price) * 100`
- Capped at +300%, rounded to 1 decimal.

**Probability %**

```
composite = 0.25 * technical
          + 0.20 * fundamental
          + 0.20 * sentiment
          + 0.20 * analyst
          + 0.15 * historical
prob = composite * 10 * confidence_multiplier  # HIGH=1.0 MED=0.82 SPEC=0.65
prob = clamp(prob, 30, 92)  # rounded to whole %
```

**Brief reasoning** (≤15 words) is auto-built from the top two scoring
signals, e.g. *"Oversold RSI + analyst upgrade in a bullish macro
environment"*.

---

## File outputs

```
results/
  daily_picks_2026-05-10.json   # one file per day
  latest.json                   # always overwritten with today's results
logs/
  apex.log                      # run log + errors
```

---

## Notes on data sources

- **Polygon.io** powers the price/volume bars, ticker reference, fundamentals
  (`/vX/reference/financials`), news headlines, and insider transactions.
- **NewsAPI** supplements Polygon news for sentiment context.
- **Anthropic Claude Opus 4** (`claude-opus-4-20250514`) generates the thesis.

If any data source fails for a single ticker, APEX logs the error and
continues with the data it has — the pipeline never crashes the whole run on
one bad fetch.
