# Scalping backtest (phase 0)

Isolated from the APEX stock engine. A scalping result is only as real as the
spread, the commission, and the slippage it paid. This package is that gate.

It does not import `apex_trader`, `continuous_backtester`, or the shadow
engine. Install its dependencies separately so APEX CI stays unchanged:

```bash
pip install -r scalping/requirements.txt
```

## What a fill costs

* **Spread** is the historical ask − bid on the tick that fills. There is no
  fixed-spread setting.
* **Commission** defaults to $3.50 per lot per side, charged on the entry and
  again on the exit (`scalping/config.json`).
* **Slippage** defaults to 2 points (a point is 0.00001 on EURUSD and 0.001 on
  USDJPY). Market orders, stop entries, and stop-losses pay it. A long
  stop-loss that is touched fills at the level minus slippage; a gap through
  the stop fills at the worse quote minus slippage. Limits and take-profits
  fill at their price.
* **P&L** is converted into EUR. A credit buys euros at the ask; a debit sells
  euros at the bid. GBPUSD and USDJPY need the EURUSD tick cache for that
  conversion.

Strategy logic sees closed M1 bars. Orders sent on a bar become active on the
next bar. Stops and targets are checked on the ticks inside the bar, so a
stop can fire before the minute closes. Same-bar path: a stop-loss is filled
before a take-profit when the stop's tick comes first. On the entry tick
itself only an adverse stop is honored.

## Data

Dukascopy publishes free hourly `.bi5` tick files (bid, ask, volumes). That is
the same feed `duka` and `dukascopy-node` download. The pipeline caches each
hour under `scalping_data/bi5/` and never requests a file that is already
there, including hours the feed returns empty. Ticks and M1 bars (mid OHLC
plus the real spread) are written to parquet partitioned by pair and year:

```text
scalping_data/ticks/pair=EURUSD/year=2018/2018-01-03.parquet
scalping_data/bars/pair=EURUSD/year=2018/2018-01-03.parquet
```

Saturday is not requested (the FX market is closed). A calendar day is written
to parquet only after every other hour of that day is cached, so a backtest
cannot silently skip a morning. Download on a machine that can reach
Dukascopy. Do not run the download in CI. The unit tests under
`scalping/tests` use synthetic ticks and a local `.bi5` fixture.

```bash
python -m scalping.data.pipeline download
python -m scalping.data.pipeline download --pair EURUSD --block 2018-19
python -m scalping.data.pipeline status
```

`--backend library` calls `dukascopy_python.fetch_ticks` instead of the `.bi5`
archive. The default HTTP session is `dukascopy_python.create_session` when
that package is installed, and the `.bi5` URL is `duka`'s template when
`duka` is installed.

Pairs and blocks live in `scalping/config.json`: EURUSD, GBPUSD, USDJPY, and
the half-open windows 2018-19 through 2023-24.

## Run

```bash
python -m scalping.run --demo
python -m scalping.run --strategy bollinger_reversion --pair EURUSD --out report.json
python -m scalping.run --strategy all
```

`--demo` scores the reference strategies on a synthetic EURUSD path so the
wiring can be checked without a download. A real run reads the parquet cache
and exits with a download hint when a block is missing.

Each block is scored by a **new** strategy instance built from the same
parameter dict. Results are not passed back in, and the dict is not updated
between blocks. Tune parameters on data you are not scoring.

`bollinger_reversion` and `donchian_breakout` are reference strategies, there
to prove the engine. Plug a real idea in by subclassing `Strategy` and
registering it in `scalping/strategies/__init__.py`. `on_bar` sees closed bars
plus open positions and returns order, cancel, close, or modify intents.

The JSON report has, per strategy per block and net of costs: net return,
profit factor, win rate, expectancy, max drawdown, trade count, and a daily
Sharpe. Across blocks it reports a consistency score: the fraction of blocks
with a positive net return, reduced when those returns are uneven.
