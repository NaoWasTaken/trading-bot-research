# OANDA Paper Trading Bot

This is a beginner-friendly Python OANDA bot built around the official OANDA v20 REST API. It supports safe OANDA practice paper-order gates and a separate historical backtest mode.

## What It Does

- Loads OANDA practice credentials from a `.env` file.
- Reads account summary and balance.
- Fetches completed candles for the configured instrument/timeframe.
- Calculates simple strategy indicators such as EMA, ATR, breakouts, and RSI.
- Generates `BUY`, `SELL`, or `HOLD` signals from the selected `STRATEGY`.
- Supports `--force-signal BUY|SELL|HOLD` for order-gate testing only.
- Only submits a paper order when `DRY_RUN=false` and the run includes `--confirm-order`.
- Runs historical backtests with pagination, spread, slippage, stop-loss simulation, buy-and-hold comparison, exposure stats, pips/R metrics, and period breakdowns.
- Compares all registered strategies over the same candle sample.
- Compares all registered strategies across a configured forex instrument list.
- Compares one selected strategy across multiple instruments and timeframes.
- Runs walk-forward tests across sequential non-overlapping date windows.
- Supports a paper forward-test mode for the selected candidate setup.
- Logs live/paper decisions to SQLite and text logs.

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your OANDA practice credentials:

```env
OANDA_API_KEY=your_token_here
OANDA_ACCOUNT_ID=your_account_id_here
OANDA_ENV=practice
DRY_RUN=true
```

The default strategy is:

```env
STRATEGY=ema_cross_v1
```

The default cross-instrument comparison list is:

```env
FOREX_INSTRUMENTS=EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CAD,NZD_USD,EUR_JPY
```

The default timeframe comparison settings are:

```env
SELECTED_COMPARE_STRATEGY=rsi_reversion_v1
TIMEFRAMES=M15,M30,H1,H4
```

The filtered RSI strategy is available as `rsi_reversion_filtered_v1`. Its regime filters are off by default:

```env
RSI_FILTER_TREND_MODE=off
RSI_FILTER_ATR_MODE=off
RSI_FILTER_ATR_MEDIAN_PERIOD=100
RSI_FILTER_SESSION=off
```

Filter modes:

- `RSI_FILTER_TREND_MODE`: `off`, `with_trend`, or `against_trend`.
- `RSI_FILTER_ATR_MODE`: `off`, `below_median`, or `above_median`.
- `RSI_FILTER_SESSION`: `off`, `london`, `new_york`, or `london_new_york`.

The default walk-forward settings are:

```env
WALK_FORWARD_INSTRUMENT=GBP_USD
WALK_FORWARD_TIMEFRAME=H4
WALK_FORWARD_STRATEGY=rsi_reversion_v1
WALK_FORWARD_START_DATE=2020-01-01
WALK_FORWARD_END_DATE=2026-04-17
WALK_FORWARD_WINDOW_MONTHS=6
```

The current forward-test candidate is:

```env
FORWARD_TEST_MODE=true
DRY_RUN=true
STRATEGY=rsi_reversion_filtered_v1
INSTRUMENT=GBP_USD
GRANULARITY=H4
RSI_FILTER_TREND_MODE=against_trend
RSI_FILTER_ATR_MODE=below_median
RSI_FILTER_SESSION=off
```

When `FORWARD_TEST_MODE=true`, the bot prints `FORWARD TEST MODE ACTIVE`, tags SQLite decision logs with `forward_test=true`, and blocks `--force-signal`. `DRY_RUN=true` remains the default unless you explicitly set `DRY_RUN=false`, and the existing order safety gates still apply.

Available strategies:

- `ema_cross_v1`: EMA20/EMA50 crossover baseline.
- `trend_filter_v1`: long-only close vs EMA200 trend filter.
- `breakout_v1`: previous-range breakout using `BREAKOUT_LOOKBACK`.
- `rsi_reversion_v1`: RSI mean reversion with optional shorts via `RSI_ALLOW_SHORTS`.
- `rsi_reversion_filtered_v1`: RSI mean reversion with optional trend, ATR, and session filters. Filters default to `off`.
- `ema_trend_filter_v1`: EMA20/EMA50 crossover filtered by EMA200.

## Commands

Run one live/paper decision cycle:

```bash
python main.py --once
```

Allow a paper order only if `.env` also has `DRY_RUN=false`:

```bash
python main.py --once --confirm-order
```

Check connectivity without logging a trade decision:

```bash
python main.py --check-connection
```

Print bot status and the last 5 SQLite decision logs:

```bash
python main.py --status
```

Print only the candidate setup and latest signal:

```bash
python main.py --candidate-status
```

Force a test signal without bypassing safety checks:

```bash
python main.py --once --force-signal BUY
```

Run a historical backtest:

```bash
python main.py --backtest
```

Run a backtest-only mode explicitly isolated from order placement:

```bash
python main.py --backtest-only
```

Compare every registered strategy over the same candles:

```bash
python main.py --compare-strategies
```

Compare every registered strategy across `FOREX_INSTRUMENTS`:

```bash
python main.py --compare-instruments
```

Compare `SELECTED_COMPARE_STRATEGY` across `FOREX_INSTRUMENTS` and `TIMEFRAMES`:

```bash
python main.py --compare-timeframes
```

Run walk-forward testing:

```bash
python main.py --walk-forward
```

Run continuously every 5 minutes:

```bash
python main.py
```

## Safety Defaults

- Only `OANDA_ENV=practice` is allowed.
- `DRY_RUN=true` is the default safety mode.
- `FORWARD_TEST_MODE=true` is paper-focused: it defaults safely through `DRY_RUN=true`, tags each decision log, and does not allow `--force-signal`.
- No paper order is allowed unless both `DRY_RUN=false` and `--confirm-order` are set.
- Pricing must be `tradeable`, spread must be within `MAX_SPREAD_PIPS`, and every order must include a stop loss.
- `--force-signal` only overrides the signal action for testing and does not bypass any safety checks.
- `--backtest`, `--backtest-only`, `--compare-strategies`, `--compare-instruments`, `--compare-timeframes`, and `--walk-forward` never place orders and ignore order-confirmation flags.
- The API key is loaded from `.env` and is never printed by the bot.

## Backtest Outputs

- Trades CSV: `storage/backtest_trades.csv`
- Summary report: `storage/backtest_report.txt`
- Strategy comparison CSV: `storage/strategy_comparison.csv`
- Instrument/strategy comparison CSV: `storage/instrument_strategy_comparison.csv`
- Timeframe/strategy comparison CSV: `storage/timeframe_strategy_comparison.csv`
- Walk-forward CSV: `storage/walk_forward_report.csv`
- Walk-forward report: `storage/walk_forward_report.txt`

Backtests use completed midpoint candles, configured spread/slippage, and the existing ATR/fallback stop-loss logic.
