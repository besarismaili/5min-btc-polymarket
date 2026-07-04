# Polymarket BTC 5-Minute Up/Down Bot

A self-contained trading bot for Polymarket's **BTC 5-minute Up/Down** markets.
Based on [Jared Davidson (@Archetapp)](https://x.com/Archetapp)'s build guide,
"Polymarket BTC 5-Minute Up/Down Trading Bot" (and its gist comments), reimplemented
here as six focused Python modules at the repo root with backtesting, dry-run
support, and an optional Playwright auto-claimer.

> **Risk warning:** This bot trades short-horizon binary prediction markets with
> real money. BTC 5-minute Up/Down markets are extremely fast, thinly modeled,
> and can move against you in seconds. Past backtest performance does not
> guarantee future results. **Always dry-run first**, start with the smallest
> possible bankroll, and never trade money you cannot afford to lose. This is
> not financial advice.

## Architecture

| Module | Role |
|---|---|
| `bot.py` | Main trading engine: per-window cycle (fetch market, determine price-to-beat, run TA loop, fire order, resolve, persist state). |
| `strategy.py` | Seven-indicator weighted signal engine (`analyze`) plus the delta-based `estimate_token_price` model used for dry-runs/backtests. |
| `backtest.py` | Fetches paginated historical 1-minute BTCUSDT candles from Binance and saves them as JSON. |
| `compare_runs.py` | Backtests 27 configs (9 confidence thresholds × 3 sizing modes) over historical candles and writes an Excel report. |
| `setup_creds.py` | One-time derivation of Polymarket CLOB API credentials from your wallet private key. |
| `auto_claim.py` | Playwright-based background safety net that clicks "Claim" on resolved positions. |

All six modules live at the repository root and only depend on the packages
pinned in `requirements.txt` (see below) — no external/private repos.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # only needed for auto_claim.py

cp .env.example .env
# edit .env: set POLY_PRIVATE_KEY (and POLY_FUNDER_ADDRESS if you use a proxy wallet)

python setup_creds.py --write-env   # derives & saves POLY_API_KEY/SECRET/PASSPHRASE

# ALWAYS dry-run first:
python bot.py --dry-run --mode safe
```

Once you're comfortable with the dry-run output, drop `--dry-run` to trade live
with real orders. Consider `--once` or `--max-trades N` for a bounded first
live test instead of an unattended loop.

## Modes

`bot.py --mode {safe,aggressive,degen}` (default from `BOT_MODE` env var):

| Mode | Min. confidence | Sizing |
|---|---|---|
| `safe` | 0.30 | 25% of current bankroll (floored at `MIN_BET`) |
| `aggressive` | 0.20 | Profits only — bets `max(bankroll − starting_bankroll, MIN_BET)`, protecting original principal |
| `degen` | 0.0 | All-in — full current bankroll every trade |

Every mode respects `MIN_BET` and `STARTING_BANKROLL` from `.env`. Confidence
and score come from `strategy.analyze`; see that module's docstring for the
full indicator breakdown (window delta, micro momentum, acceleration, EMA
9/21, RSI 14, volume surge, real-time tick trend).

## Backtesting

Fetch history and run the 27-config comparison sweep:

```bash
python backtest.py --hours 72 --output candles.json
python compare_runs.py --candles candles.json --output results.xlsx
# or fetch on the fly:
python compare_runs.py --hours 72 --output results.xlsx
```

`results.xlsx` has three sheets:
- **Summary** — one row per config (threshold × sizing mode), sorted by final bankroll.
- **Best Config Trades** — full trade log of the best-performing config.
- **Bankroll Curves** — bankroll after each window, one column per config.

Use this to sanity-check a mode/threshold before ever running it live.

## Auto-claim (safety net)

Most 5-minute markets auto-settle and pay out automatically. `auto_claim.py`
is a Playwright-based safety net for the rare position that needs a manual
claim:

```bash
python auto_claim.py --setup     # headed browser, log into Polymarket once
python auto_claim.py             # headless loop, checks every --interval (default 300s)
python auto_claim.py --once      # single pass
```

It uses a persistent browser profile (`--profile-dir`, default
`runtime/pw-profile`) so you only log in once. It's robust to Polymarket UI
changes — missing "Claim" buttons are logged and skipped, never fatal.

## Configuration reference

See `.env.example` for the full list of environment variables
(`POLY_PRIVATE_KEY`, `POLY_API_KEY`/`SECRET`/`PASSPHRASE`,
`POLY_FUNDER_ADDRESS`, `POLY_SIGNATURE_TYPE`, `STARTING_BANKROLL`, `MIN_BET`,
`BOT_MODE`), loaded via `python-dotenv` from `.env`.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

Tests are fully offline: all HTTP/WebSocket calls and the CLOB client are
stubbed/mocked.

## Runtime data

`bot.py` creates a `runtime/` directory (git-ignored) holding
`bot_state.json` (bankroll/trade counters) and `trades.jsonl` (per-trade log).
`auto_claim.py` stores its browser profile under `runtime/pw-profile/`.

---

## Legacy skill wrapper (scripts/)

The repository also ships an older, thinner "OpenClaw skill" wrapper under
`scripts/` and `config/`. It does **not** place orders itself — it delegates
execution to a separate/private trading repo
(`pm_live_trade_runner.py` in `<your-workspace>/pm-hl-conservative-plus-repo`)
and is kept only for backward compatibility with existing OpenClaw
automations. New work should use the self-contained bot described above.

Summary of the legacy layout:
- `SKILL.md` — skill definition and operating rules for the OpenClaw agent.
- `config/btc_5m_profiles.yaml` — `conservative`/`aggressive` profiles (entry
  timing, staleness/spread/liquidity guards, hedge triggers, risk caps).
- `scripts/btc5m_ctl.sh` — unified control entrypoint (`start|status|stop|report|logs`).
- `scripts/btc5m_hot.sh` — chat-friendly hot-command handler.
- `scripts/test_btc_5m_session_exit_sl.py` — canonical runner (forwards to the
  external execution repo).
- `scripts/run_btc_5m_threshold_test.py` — deprecated compatibility wrapper.
- `scripts/btc5m_report.py` / `scripts/btc5m_latest_report.py` — PnL/report utilities.
- `scripts/btc5m_docker.sh` — optional Docker isolation.

Legacy quick start (requires the external execution repo and its own `.env`):

```bash
git clone https://github.com/Novals83/5min-btc-polymarket.git
cd 5min-btc-polymarket
scripts/btc5m_ctl.sh start --profile conservative
scripts/btc5m_ctl.sh status
scripts/btc5m_ctl.sh report --limit 20
scripts/btc5m_ctl.sh stop
```

The legacy wrapper's strategy is momentum-into-close (enter ~2 minutes before
expiry once BTC has moved ~$70-100, in the direction supported by market
skew, sized around 50% of allocation with an optional micro-hedge on extreme
skew). Same risk warning applies: educational/operational infrastructure, not
financial advice — use your own risk limits and capital controls.
