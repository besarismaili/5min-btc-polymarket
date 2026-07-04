"""Binance historical 1-minute candle fetcher for backtesting.

Paginates the Binance klines REST API (1000 candles per request max) to build a
history of Candle dicts going back a requested number of hours. Provides a small
CLI to save the result as JSON.

Candle format (shared): ``dict`` with keys ``ts`` (int, ms epoch of candle
open), ``open``, ``high``, ``low``, ``close``, ``volume`` (floats).
"""

from __future__ import annotations

import argparse
import json
import logging
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Milliseconds per 1-minute interval; used to compute pagination windows.
_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

_MAX_LIMIT = 1000


def _kline_to_candle(k: list) -> dict:
    """Convert a raw Binance kline array into a Candle dict."""
    return {
        "ts": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
    }


def fetch_history(hours: float, symbol: str = "BTCUSDT", interval: str = "1m",
                  base_url: str = "https://api.binance.com") -> list[dict]:
    """Paginate klines (1000/req) back ``hours`` from now.

    Returns a list of Candle dicts sorted ascending by ``ts`` with duplicates
    removed.
    """
    interval_ms = _INTERVAL_MS.get(interval)
    if interval_ms is None:
        raise ValueError(f"Unsupported interval: {interval}")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(hours * 3_600_000)

    url = f"{base_url}/api/v3/klines"
    candles: dict[int, dict] = {}
    cursor = start_ms

    while cursor < now_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": now_ms,
            "limit": _MAX_LIMIT,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for k in batch:
            candle = _kline_to_candle(k)
            candles[candle["ts"]] = candle
        last_open = int(batch[-1][0])
        # Advance past the last candle we received to avoid re-fetching it.
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            # No forward progress; stop to avoid an infinite loop.
            break
        cursor = next_cursor
        if len(batch) < _MAX_LIMIT:
            # Reached the end of available data.
            break
        # Be polite to the API.
        time.sleep(0.2)

    result = [candles[ts] for ts in sorted(candles)]
    logger.info("Fetched %d candles for %s %s over %.1fh",
                len(result), symbol, interval, hours)
    return result


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch Binance historical 1m candles for backtesting.")
    parser.add_argument("--hours", type=float, default=72.0,
                        help="How many hours of history to fetch (default 72).")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--base-url", default="https://api.binance.com")
    parser.add_argument("--output", default="candles.json",
                        help="Path to save the JSON candle list.")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    candles = fetch_history(args.hours, symbol=args.symbol,
                            interval=args.interval, base_url=args.base_url)
    with open(args.output, "w") as f:
        json.dump(candles, f)
    logger.info("Saved %d candles to %s", len(candles), args.output)


if __name__ == "__main__":
    main()
