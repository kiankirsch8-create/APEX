# APEX — Autonomous Stock Discovery Engine

APEX is a Python backend that scans the entire US stock market every morning at 07:00, finds the highest-probability explosive opportunities in **speculative small/micro caps** and **massively undervalued large caps**, scores them with a Claude-powered analyst, and serves the full report through a FastAPI backend ready for a Lovable frontend.

The user never picks the stock. **The machine finds it.**

---

## What APEX does

1. **Two screeners run in parallel** every morning over a curated **400-ticker universe** (300 small/micro caps + 100 large caps in `universe.py`), all data via free **yfinance** (no API key, no rate limit):
   - `screener_small_caps.py` — explosive setups in $10M–$2B names (volume spikes, oversold bounces, short-squeeze setups, catalyst-driven plays, insider buying, etc.)
   - `screener_big_players.py` — undervalued giants ($2B+) with deep value, earnings inflection, sentiment mismatch, sector-rotation tailwinds, etc.
2. **Top 3 small caps + top 2 big players** are passed into `analyzer.py`, which calls the **Claude `claude-opus-4-20250514`** model with a sophisticated APEX system prompt.
3. **`scorer.py`** validates upside, computes a probability between 25%–91%, generates a punchy reasoning sentence, and produces a strict **position-sizing recommendation** that obeys all per-position and portfolio-level caps.
4. Results are persisted under `results/latest.json` and `results/daily_picks_YYYY-MM-DD.json` and exposed through a FastAPI server.

---

## Project layout

```
master_prompt.py        — the APEX analyst system prompt
market_data.py          — async yfinance client + technicals + NewsAPI
universe.py             — curated 300 small caps + 100 large caps to scan
screener_small_caps.py  — speculative explosive small/micro cap scanner
screener_big_players.py — undervalued large cap scanner
analyzer.py             — Claude-powered full report generator
scorer.py               — upside %, probability %, position sizing
scheduler.py            — daily runner (07:00 local) + --run-now flag
api.py                  — FastAPI backend for the Lovable frontend
utils.py                — logging + JSON IO helpers
requirements.txt        — Python dependencies
.env.example            — required environment variables
results/                — daily picks JSON files (auto-created)
logs/                   — apex.log (auto-created)
config/                 — persisted budget config (auto-created)
```

---

## API keys you need

| Service | Env var | Get a key |
| --- | --- | --- |
| Anthropic (Claude Opus 4) | `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys> |
| NewsAPI.org (headlines) | `NEWSAPI_KEY` | <https://newsapi.org/account> |

**Market data is provided by free [yfinance](https://github.com/ranaroussi/yfinance) — no API key, no rate limits.** NewsAPI is optional; if `NEWSAPI_KEY` is missing the analyzer still runs using only yfinance news.

---

## Setup

```bash
git clone <this-repo>
cd apex
cp .env.example .env
# edit .env and paste your real keys

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` example:

```env
ANTHROPIC_API_KEY=sk-ant-...
NEWSAPI_KEY=...
PORT=8000
TIMEZONE=Europe/Berlin
DEFAULT_BUDGET_USD=10000
```

---

## Running APEX

### One-shot pipeline (test or manual run)

```bash
python scheduler.py --run-now
```

This runs both screeners, analyzes the candidates, scores them, and writes `results/latest.json`.

### Daily 07:00 scheduler (production)

```bash
python scheduler.py
```

Blocks the process and runs the pipeline every day at 07:00 local time.

### Start the FastAPI server

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Then point your Lovable frontend at `http://localhost:8000`.

---

## API endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Liveness probe — `{"status": "ok"}` |
| GET | `/api/latest` | Today's picks: small caps + big players + top pick |
| GET | `/api/pick/{ticker}` | Single pick from today's results |
| GET | `/api/history` | List of all dates that have saved picks |
| GET | `/api/history/{date}` | Picks for a specific `YYYY-MM-DD` |
| POST | `/api/analyze/{ticker}?section=SMALL_CAP\|BIG_PLAYER` | On-demand full analysis |
| POST | `/api/run` | Manually trigger today's full pipeline |
| GET | `/api/status` | Run status, last run timestamp, next run, current budget |
| GET | `/api/budget` | Read the saved investment budget |
| POST | `/api/budget` | Save a new total investment budget (`{"total_budget_usd": 25000}`) |

---

## Position sizing rules

`scorer.py` enforces these strictly — even if the LLM tries to suggest more aggressive sizing.

| Rating | Max single position | Notes |
| --- | --- | --- |
| `SPECULATIVE BUY` | 5% (2% if probability < 55%) | Small-cap risk capped |
| `BUY` / `STRONG BUY` | 8% → 15% | Linear with probability between 60% and 85% |
| `SHORT` / `STRONG SHORT` | 3% | Short-side risk capped |
| `AVOID` | 0% | |

Hard rules:
- No single position > 15% of total budget
- Combined long allocation across all picks ≤ 35% of total budget (proportionally scaled if exceeded)

---

## Output schema (per pick)

Each pick in `/api/latest` matches the schema documented in `analyzer.py`. Highlights:

- `apex_rating` — `STRONG BUY`, `BUY`, `SPECULATIVE BUY`, `AVOID`, `SHORT`, `STRONG SHORT`
- `direction` — `UP` or `DOWN`
- `target_30d`, `target_90d`, `target_12m`, `stop_loss`
- `final_upside_percentage`, `final_probability_percentage`, `final_reasoning`
- `composite_score`, `confidence_level`, individual factor scores (macro/technical/fundamental/sentiment/analyst/historical)
- `thesis`, `verdict`, `risks`, `historical_analog`, `catalysts`
- `position_sizing` with `recommended_invest_amount`, `recommended_invest_percentage`, `potential_return_dollars`, `potential_loss_dollars`, `risk_reward_ratio`, `sizing_reasoning`, `risk_category`

---

## Logs

All errors and pipeline events are written to `logs/apex.log` (rotating, 5MB × 5 files) and echoed to stdout. Every external API call is wrapped in try/except — a single failure can never crash the pipeline.
