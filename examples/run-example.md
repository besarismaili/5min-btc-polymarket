# Example commands

## Self-contained bot (repo root) — recommended

One-time setup:

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in POLY_PRIVATE_KEY, etc.
python setup_creds.py --write-env
```

Dry-run (safe validation, no orders placed):

```bash
python bot.py --dry-run --mode safe
```

Single dry-run window then exit:

```bash
python bot.py --dry-run --mode safe --once
```

Live trading (real orders — start small and only after dry-run validation):

```bash
python bot.py --mode safe
python bot.py --mode aggressive --max-trades 5
python bot.py --mode degen --once
```

Override the confidence threshold for a single run:

```bash
python bot.py --dry-run --mode safe --min-confidence 0.4
```

Backtesting:

```bash
python backtest.py --hours 72 --output candles.json
python compare_runs.py --candles candles.json --output results.xlsx
```

Auto-claim safety net:

```bash
python auto_claim.py --setup     # once, headed, to log in
python auto_claim.py --once      # single claim pass
python auto_claim.py --interval 120
```

---

## Legacy skill wrapper (scripts/)

Dry-run (safe validation):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative
```

Real execution (conservative):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Real execution (aggressive):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```
