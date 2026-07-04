#!/usr/bin/env python3
"""Polymarket BTC 5-minute Up/Down trading bot — main engine.

Based on Jared Davidson (@Archetapp)'s build guide. This module is the
self-contained trading engine: it discovers the current 5-minute BTC Up/Down
market on Polymarket (via the Gamma API), determines the "price to beat" from
the Chainlink live-data websocket (with a Binance kline fallback), runs a
short technical-analysis loop in the final seconds before window close, places
an order (or simulates one in dry-run), resolves the outcome and persists
bankroll/trade state.

The engine is deliberately built around a few injectable seams so it can be
exercised entirely offline in tests:

* ``Clock``           — wall-clock time + sleeping (``FakeClock`` in tests).
* ``MarketDataProvider`` — Gamma / Binance HTTP access (stubbed in tests).
* ``ChainlinkPriceFeed`` — the websocket price feed (optional; may be ``None``).
* ``PolymarketExecutor`` — all py-clob-client usage (stubbed in tests).
* ``strategy``        — the ``analyze`` / ``estimate_token_price`` module
                        (monkeypatched / injected in tests).

Only the real implementations touch the network; every network call is wrapped
so a transient failure never crashes the trading loop.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

try:  # optional dependency; only needed for live/real runs
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv always installed here
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


LOG = logging.getLogger("btc5m.bot")

# --------------------------------------------------------------------------- #
# Constants (article-derived timing / order rules)
# --------------------------------------------------------------------------- #
WINDOW_SECONDS = 300          # each market covers a 5-minute window
ENTRY_OFFSET = 10             # begin the TA loop at T-10s
DEADLINE_OFFSET = 5           # hard fire deadline at T-5s (never skip past this)
TA_POLL_SECONDS = 2           # poll TA every 2s during the loop
SPIKE_SCORE_JUMP = 1.5        # fire early if score jumps this much between checks
FOK_RETRY_SECONDS = 3         # retry FOK market buys every 3s until T-0
GTC_FALLBACK_PRICE = 0.95     # GTC limit-buy price when FOK cannot fill
MIN_ORDER_SHARES = 5.0        # minimum order size (shares)
RESOLUTION_DELAY = 10         # wait this long after close before resolving

GAMMA_BASE = "https://gamma-api.polymarket.com"
BINANCE_BASE = "https://api.binance.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAINLINK_WS = "wss://ws-live-data.polymarket.com"

MODE_MIN_CONFIDENCE = {"safe": 0.30, "aggressive": 0.20, "degen": 0.0}


# --------------------------------------------------------------------------- #
# Deterministic window / slug math
# --------------------------------------------------------------------------- #
def window_ts_for(now_epoch: float) -> int:
    """Return the epoch-second open of the 5-minute window containing ``now``."""
    now = int(now_epoch)
    return now - (now % WINDOW_SECONDS)


def window_close_ts(window_ts: int) -> int:
    """Epoch second at which the given window closes."""
    return window_ts + WINDOW_SECONDS


def slug_for(window_ts: int) -> str:
    """Polymarket event slug for a given window open timestamp."""
    return f"btc-updown-5m-{window_ts}"


# --------------------------------------------------------------------------- #
# Mode policy (min confidence + stake sizing)
# --------------------------------------------------------------------------- #
def mode_min_confidence(mode: str) -> float:
    return MODE_MIN_CONFIDENCE.get(mode, 0.30)


def mode_stake(mode: str, bankroll: float, starting_bankroll: float,
               min_bet: float) -> float:
    """Compute the stake (USDC) for one trade given the bankroll mode.

    * safe:       25% of bankroll, floored at MIN_BET.
    * aggressive: only the profit above the starting bankroll (protect
                  principal), floored at MIN_BET.
    * degen:      the full bankroll.

    The result is always capped at the available bankroll.
    """
    if mode == "safe":
        stake = max(bankroll * 0.25, min_bet)
    elif mode == "aggressive":
        stake = max(bankroll - starting_bankroll, min_bet)
    elif mode == "degen":
        stake = bankroll
    else:  # defensive default -> behave like safe
        stake = max(bankroll * 0.25, min_bet)
    return min(stake, bankroll)


# --------------------------------------------------------------------------- #
# Fire decision logic (pure & testable)
# --------------------------------------------------------------------------- #
def evaluate_fire(prev_score: Optional[float], signal: dict, min_conf: float,
                  at_deadline: bool,
                  spike_threshold: float = SPIKE_SCORE_JUMP) -> tuple[bool, Optional[str]]:
    """Decide whether to fire the order now.

    Returns ``(should_fire, reason)`` where reason is one of
    ``"deadline" | "spike" | "confidence" | None``.

    * At the hard deadline we always fire (article lesson #3: never skip).
    * Otherwise fire early on a score spike (jump >= threshold vs the previous
      check) or once confidence reaches the mode's minimum.
    """
    if at_deadline:
        return True, "deadline"
    if prev_score is not None and abs(signal["score"] - prev_score) >= spike_threshold:
        return True, "spike"
    if signal["confidence"] >= min_conf:
        return True, "confidence"
    return False, None


def resolve_outcome(window_open: float, close_price: float) -> str:
    """Winning side given the window open (price-to-beat) and its close."""
    return "UP" if close_price >= window_open else "DOWN"


def compute_pnl(side: str, winning_side: str, entry_price: float,
                shares: float) -> tuple[str, float]:
    """Return ``(outcome, pnl)`` for a resolved trade.

    Win  -> +shares * (1 - entry_price)
    Lose -> -shares * entry_price
    """
    if side == winning_side:
        return "win", shares * (1.0 - entry_price)
    return "loss", -shares * entry_price


# --------------------------------------------------------------------------- #
# Clock seam
# --------------------------------------------------------------------------- #
class Clock:
    """Real wall-clock. Tests inject a fake with the same interface."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Chainlink price feed (websocket, background thread, ring buffer)
# --------------------------------------------------------------------------- #
class ChainlinkPriceFeed:
    """Background websocket listener for Polymarket's Chainlink BTC/USD feed.

    Connects to ``wss://ws-live-data.polymarket.com`` and subscribes to the
    ``crypto_prices_chainlink`` topic filtered to ``BTC/USD``. Incoming update
    messages carry a ``payload`` with ``value`` (price) and ``timestamp`` (ms).
    Ticks are kept in a bounded ring buffer. The socket auto-reconnects.

    Purely a data source; it is never started in offline tests.
    """

    SUBSCRIBE_MSG = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "filters": '{"symbol":"BTC/USD"}',
            }
        ],
    }

    def __init__(self, url: str = CHAINLINK_WS, buffer_size: int = 2048):
        self.url = url
        self._buf: "deque[tuple[int, float]]" = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="chainlink-feed",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    def _run(self) -> None:  # pragma: no cover - network loop
        import websocket  # local import so tests never need the dep at import

        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                LOG.warning("Chainlink feed error: %s", exc)
            if self._running:
                time.sleep(2)  # brief backoff before reconnect

    # -- callbacks -------------------------------------------------------- #
    def _on_open(self, ws) -> None:  # pragma: no cover - network
        try:
            ws.send(json.dumps(self.SUBSCRIBE_MSG))
            LOG.info("Chainlink feed subscribed to BTC/USD")
        except Exception as exc:
            LOG.warning("Chainlink subscribe failed: %s", exc)

    def _on_message(self, ws, message) -> None:  # pragma: no cover - network
        try:
            data = json.loads(message)
        except Exception:
            return
        payloads = data if isinstance(data, list) else [data]
        for item in payloads:
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            value = payload.get("value")
            ts = payload.get("timestamp")
            if value is None:
                continue
            try:
                self.record_tick(int(ts) if ts is not None else int(time.time() * 1000),
                                 float(value))
            except (TypeError, ValueError):
                continue

    def _on_error(self, ws, error) -> None:  # pragma: no cover - network
        LOG.warning("Chainlink ws error: %s", error)

    def _on_close(self, ws, *_a) -> None:  # pragma: no cover - network
        LOG.info("Chainlink ws closed")

    # -- data access (tested directly) ------------------------------------ #
    def record_tick(self, ts_ms: int, value: float) -> None:
        """Store a tick. Exposed so tests can seed the buffer without a socket."""
        with self._lock:
            self._buf.append((int(ts_ms), float(value)))

    def latest(self) -> Optional[float]:
        with self._lock:
            return self._buf[-1][1] if self._buf else None

    def recent_values(self, count: int) -> list[float]:
        with self._lock:
            return [v for _, v in list(self._buf)[-count:]]

    def price_at(self, ts_ms: int, tolerance_ms: int = 5000) -> Optional[float]:
        """Return the tick value nearest ``ts_ms`` within ``tolerance_ms``."""
        with self._lock:
            if not self._buf:
                return None
            best_val = None
            best_diff = None
            for t, v in self._buf:
                diff = abs(t - ts_ms)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_val = v
            if best_diff is not None and best_diff <= tolerance_ms:
                return best_val
            return None


# --------------------------------------------------------------------------- #
# Market data provider (Gamma + Binance) — the real network implementation
# --------------------------------------------------------------------------- #
class MarketDataProvider:
    """Interface for market data. Tests supply a stub with these methods."""

    def get_gamma_market(self, slug: str) -> Optional[dict]:
        raise NotImplementedError

    def get_candles(self, limit: int = 30) -> list[dict]:
        raise NotImplementedError

    def get_ticker_price(self) -> Optional[float]:
        raise NotImplementedError

    def get_price_to_beat(self, window_ts: int) -> Optional[float]:
        raise NotImplementedError

    def get_close_price(self, window_ts: int) -> Optional[float]:
        raise NotImplementedError

    def get_gamma_outcome_prices(self, slug: str) -> Optional[dict]:
        raise NotImplementedError


def _parse_gamma_event(events: list) -> Optional[dict]:
    """Turn a Gamma ``/events`` response into a normalized market dict."""
    if not events:
        return None
    event = events[0]
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]

    def _load(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return None
        return v

    token_ids = _load(market.get("clobTokenIds")) or []
    outcomes = _load(market.get("outcomes")) or []
    outcome_prices = _load(market.get("outcomePrices")) or []

    up_token = down_token = None
    price_map: dict[str, float] = {}
    for i, label in enumerate(outcomes):
        norm = str(label).strip().lower()
        tok = token_ids[i] if i < len(token_ids) else None
        if norm == "up":
            up_token = tok
        elif norm == "down":
            down_token = tok
        if i < len(outcome_prices):
            try:
                price_map[str(label).strip().title()] = float(outcome_prices[i])
            except (TypeError, ValueError):
                pass

    return {
        "slug": event.get("slug"),
        "up_token": up_token,
        "down_token": down_token,
        "outcomes": outcomes,
        "outcome_prices": price_map,
        "end_date": market.get("endDate") or event.get("endDate"),
        "raw": event,
    }


class LiveMarketData(MarketDataProvider):
    """Real Gamma + Binance HTTP data source. Every call is failure-tolerant."""

    def __init__(self, gamma_base: str = GAMMA_BASE, binance_base: str = BINANCE_BASE,
                 timeout: float = 8.0):
        self.gamma_base = gamma_base
        self.binance_base = binance_base
        self.timeout = timeout

    def _get_json(self, url: str, params: Optional[dict] = None) -> Any:
        import requests
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            LOG.warning("HTTP GET failed (%s): %s", url, exc)
            return None

    def get_gamma_market(self, slug: str) -> Optional[dict]:
        data = self._get_json(f"{self.gamma_base}/events", {"slug": slug})
        if not isinstance(data, list):
            return None
        return _parse_gamma_event(data)

    def get_gamma_outcome_prices(self, slug: str) -> Optional[dict]:
        market = self.get_gamma_market(slug)
        return market.get("outcome_prices") if market else None

    def _klines(self, limit: int, start_ms: Optional[int] = None,
                end_ms: Optional[int] = None) -> Optional[list]:
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        data = self._get_json(f"{self.binance_base}/api/v3/klines", params)
        return data if isinstance(data, list) else None

    @staticmethod
    def _kline_to_candle(k: list) -> dict:
        return {
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }

    def get_candles(self, limit: int = 30) -> list[dict]:
        raw = self._klines(limit)
        if not raw:
            return []
        try:
            return [self._kline_to_candle(k) for k in raw]
        except (IndexError, ValueError, TypeError):
            return []

    def get_ticker_price(self) -> Optional[float]:
        data = self._get_json(f"{self.binance_base}/api/v3/ticker/price",
                              {"symbol": "BTCUSDT"})
        if isinstance(data, dict) and "price" in data:
            try:
                return float(data["price"])
            except (TypeError, ValueError):
                return None
        return None

    def get_price_to_beat(self, window_ts: int) -> Optional[float]:
        start_ms = window_ts * 1000
        raw = self._klines(1, start_ms=start_ms, end_ms=start_ms + 60_000)
        if raw:
            try:
                return float(raw[0][1])  # open of the window's first 1m candle
            except (IndexError, ValueError, TypeError):
                return None
        return None

    def get_close_price(self, window_ts: int) -> Optional[float]:
        """Close of the last 1m candle inside [window_ts, window_ts+300)."""
        last_min = window_ts + WINDOW_SECONDS - 60
        start_ms = last_min * 1000
        raw = self._klines(1, start_ms=start_ms, end_ms=start_ms + 60_000)
        if raw:
            try:
                return float(raw[0][4])  # close
            except (IndexError, ValueError, TypeError):
                return None
        return None


# --------------------------------------------------------------------------- #
# Polymarket executor — isolates all py-clob-client usage
# --------------------------------------------------------------------------- #
class PolymarketExecutor:
    """Thin wrapper over py-clob-client so the order path can be stubbed.

    Only used in live mode. Every method is best-effort and returns structured
    results instead of raising, so the trading loop never crashes on a
    transient exchange error.
    """

    def __init__(self, client: Any = None):
        self._client = client

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "PolymarketExecutor":
        env = env or os.environ
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        client = ClobClient(
            CLOB_HOST,
            key=env.get("POLY_PRIVATE_KEY"),
            chain_id=137,
            signature_type=int(env.get("POLY_SIGNATURE_TYPE", "1")),
            funder=env.get("POLY_FUNDER_ADDRESS"),
        )
        client.set_api_creds(ApiCreds(
            env.get("POLY_API_KEY"),
            env.get("POLY_API_SECRET"),
            env.get("POLY_API_PASSPHRASE"),
        ))
        return cls(client)

    def market_buy_fok(self, token_id: str, amount_usdc: float) -> dict:
        """Attempt a FOK market buy. Returns ``{"filled": bool, ...}``."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            args = MarketOrderArgs(token_id=token_id, amount=float(amount_usdc),
                                   side=BUY)
            order = self._client.create_market_order(args)
            resp = self._client.post_order(order, OrderType.FOK)
            filled = bool(resp and resp.get("success", True) and
                          resp.get("status") not in ("unmatched", "cancelled"))
            return {"filled": filled, "response": resp, "amount": amount_usdc}
        except Exception as exc:
            LOG.warning("FOK market buy failed: %s", exc)
            return {"filled": False, "error": str(exc)}

    def limit_buy_gtc(self, token_id: str, price: float, shares: float) -> dict:
        """Place a GTC limit buy. Returns ``{"order_id": ..., ...}``."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            args = OrderArgs(token_id=token_id, price=float(price),
                             size=float(shares), side=BUY)
            order = self._client.create_order(args)
            resp = self._client.post_order(order, OrderType.GTC)
            order_id = resp.get("orderID") if isinstance(resp, dict) else None
            return {"order_id": order_id, "response": resp, "shares": shares,
                    "price": price}
        except Exception as exc:
            LOG.warning("GTC limit buy failed: %s", exc)
            return {"order_id": None, "error": str(exc)}

    def order_filled_shares(self, order_id: str) -> float:
        try:
            order = self._client.get_order(order_id)
            if isinstance(order, dict):
                return float(order.get("size_matched", 0) or 0)
        except Exception as exc:
            LOG.warning("get_order failed: %s", exc)
        return 0.0

    def cancel(self, order_id: str) -> None:
        try:
            self._client.cancel(order_id)
        except Exception as exc:
            LOG.warning("cancel failed: %s", exc)


# --------------------------------------------------------------------------- #
# Persistent bot state
# --------------------------------------------------------------------------- #
class BotState:
    def __init__(self, bankroll: float, starting_bankroll: float,
                 trades: int = 0, wins: int = 0):
        self.bankroll = bankroll
        self.starting_bankroll = starting_bankroll
        self.trades = trades
        self.wins = wins

    def to_dict(self) -> dict:
        return {
            "bankroll": self.bankroll,
            "starting_bankroll": self.starting_bankroll,
            "trades": self.trades,
            "wins": self.wins,
        }

    @classmethod
    def load(cls, path: str, starting_bankroll: float) -> "BotState":
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            return cls(
                bankroll=float(data.get("bankroll", starting_bankroll)),
                starting_bankroll=float(data.get("starting_bankroll", starting_bankroll)),
                trades=int(data.get("trades", 0)),
                wins=int(data.get("wins", 0)),
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return cls(bankroll=starting_bankroll,
                       starting_bankroll=starting_bankroll)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# The trading bot
# --------------------------------------------------------------------------- #
class TradingBot:
    def __init__(self, mode: str = "safe", dry_run: bool = True,
                 data: Optional[MarketDataProvider] = None,
                 executor: Optional[PolymarketExecutor] = None,
                 feed: Optional[ChainlinkPriceFeed] = None,
                 clock: Optional[Clock] = None,
                 strategy: Any = None,
                 starting_bankroll: float = 1.0,
                 min_bet: float = 1.0,
                 min_confidence: Optional[float] = None,
                 runtime_dir: str = "runtime"):
        self.mode = mode
        self.dry_run = dry_run
        self.data = data
        self.executor = executor
        self.feed = feed
        self.clock = clock or Clock()
        self._strategy = strategy
        self.min_bet = min_bet
        self.runtime_dir = runtime_dir
        self.state_path = os.path.join(runtime_dir, "bot_state.json")
        self.trades_path = os.path.join(runtime_dir, "trades.jsonl")
        self.state = BotState.load(self.state_path, starting_bankroll)
        # override min confidence if given, else mode default
        self.min_confidence = (min_confidence if min_confidence is not None
                               else mode_min_confidence(mode))
        self._stop = False

    # -- dependency access ------------------------------------------------ #
    @property
    def strategy(self) -> Any:
        if self._strategy is None:
            import strategy as _strategy  # lazy so tests never need the file
            self._strategy = _strategy
        return self._strategy

    def request_stop(self) -> None:
        self._stop = True

    # -- timing helper ---------------------------------------------------- #
    def _sleep_until(self, target_epoch: float) -> None:
        while not self._stop:
            remaining = target_epoch - self.clock.now()
            if remaining <= 0:
                return
            self.clock.sleep(min(remaining, TA_POLL_SECONDS))

    # -- price-to-beat ---------------------------------------------------- #
    def determine_price_to_beat(self, window_ts: int) -> Optional[float]:
        """Prefer the Chainlink WS value at the window boundary; fall back to
        the Binance 1m kline open at ``window_ts``."""
        if self.feed is not None:
            val = self.feed.price_at(window_ts * 1000, tolerance_ms=5000)
            if val is not None:
                return val
        try:
            return self.data.get_price_to_beat(window_ts)
        except Exception as exc:
            LOG.warning("price-to-beat fallback failed: %s", exc)
            return None

    # -- TA loop ---------------------------------------------------------- #
    def run_ta_loop(self, window_ts: int, window_open: float) -> dict:
        """Poll TA from T-10s until firing. Returns the fire decision dict."""
        entry_time = window_ts + WINDOW_SECONDS - ENTRY_OFFSET
        deadline = window_ts + WINDOW_SECONDS - DEADLINE_OFFSET

        self._sleep_until(entry_time)

        ticks: list[float] = []
        best: Optional[dict] = None
        prev_score: Optional[float] = None
        fired: Optional[dict] = None

        # seed ticks with any WS history we already have
        if self.feed is not None:
            ticks.extend(self.feed.recent_values(30))

        while not self._stop:
            now = self.clock.now()
            at_deadline = now >= deadline

            try:
                candles = self.data.get_candles(limit=30)
            except Exception as exc:
                LOG.warning("get_candles failed: %s", exc)
                candles = []
            try:
                price = self.data.get_ticker_price()
            except Exception as exc:
                LOG.warning("get_ticker_price failed: %s", exc)
                price = None

            if self.feed is not None:
                fv = self.feed.latest()
                if fv is not None:
                    ticks.append(fv)
                    price = price if price is not None else fv
            elif price is not None:
                ticks.append(price)

            cur_price = price if price is not None else window_open
            signal = self.strategy.analyze(candles, window_open, cur_price,
                                           ticks=list(ticks))
            if best is None or abs(signal["score"]) > abs(best["score"]):
                best = signal

            fire, reason = evaluate_fire(prev_score, signal, self.min_confidence,
                                         at_deadline)
            if fire:
                chosen = best if reason == "deadline" else signal
                fired = {
                    "signal": chosen,
                    "reason": reason,
                    "price": cur_price,
                    "ticks": list(ticks),
                }
                break

            prev_score = signal["score"]
            self.clock.sleep(TA_POLL_SECONDS)

        if fired is None:  # only when stopped mid-loop
            fired = {"signal": best or {"score": 0.0, "confidence": 0.0,
                                        "side": "UP", "components": {}},
                     "reason": "stopped", "price": window_open, "ticks": ticks}
        return fired

    # -- order execution (live) ------------------------------------------ #
    def execute_live_order(self, token_id: str, stake: float,
                           window_ts: int) -> dict:
        """FOK market buy with retries, GTC $0.95 fallback. Returns fill dict."""
        close = window_close_ts(window_ts)
        while self.clock.now() < close and not self._stop:
            result = self.executor.market_buy_fok(token_id, stake)
            if result.get("filled"):
                shares = stake / self._effective_entry_price(result)
                return {"filled": True, "shares": shares,
                        "entry_price": self._effective_entry_price(result),
                        "method": "FOK"}
            self.clock.sleep(FOK_RETRY_SECONDS)

        # FOK never filled -> GTC limit fallback
        shares = max(MIN_ORDER_SHARES, math.floor(stake / GTC_FALLBACK_PRICE * 100) / 100)
        order = self.executor.limit_buy_gtc(token_id, GTC_FALLBACK_PRICE, shares)
        order_id = order.get("order_id")
        filled_shares = 0.0
        if order_id:
            self.clock.sleep(FOK_RETRY_SECONDS)
            filled_shares = self.executor.order_filled_shares(order_id)
            if filled_shares < shares:
                self.executor.cancel(order_id)
        return {"filled": filled_shares > 0, "shares": filled_shares,
                "entry_price": GTC_FALLBACK_PRICE, "method": "GTC"}

    @staticmethod
    def _effective_entry_price(fok_result: dict) -> float:
        resp = fok_result.get("response")
        if isinstance(resp, dict):
            for key in ("avg_price", "price", "avgPrice"):
                if resp.get(key):
                    try:
                        return float(resp[key])
                    except (TypeError, ValueError):
                        pass
        # market fill price unknown — approximate slightly below the cap
        return GTC_FALLBACK_PRICE

    # -- resolution ------------------------------------------------------- #
    def resolve(self, window_ts: int, window_open: float, slug: str) -> dict:
        """Determine the winning side & close price for the window."""
        close_ms = window_close_ts(window_ts) * 1000

        close_price = None
        if self.feed is not None:
            close_price = self.feed.price_at(close_ms, tolerance_ms=8000)
        if close_price is None:
            try:
                close_price = self.data.get_close_price(window_ts)
            except Exception as exc:
                LOG.warning("get_close_price failed: %s", exc)

        if close_price is not None:
            winner = resolve_outcome(window_open, close_price)
            return {"winner": winner, "close_price": close_price,
                    "source": "price", "resolved": True}

        # Gamma outcomePrices fallback
        try:
            prices = self.data.get_gamma_outcome_prices(slug)
        except Exception as exc:
            LOG.warning("gamma outcome prices failed: %s", exc)
            prices = None
        if prices:
            up = prices.get("Up", 0.0)
            down = prices.get("Down", 0.0)
            if up >= 0.99:
                return {"winner": "UP", "close_price": None,
                        "source": "gamma", "resolved": True}
            if down >= 0.99:
                return {"winner": "DOWN", "close_price": None,
                        "source": "gamma", "resolved": True}
        return {"winner": None, "close_price": None, "source": "none",
                "resolved": False}

    # -- one full window cycle ------------------------------------------- #
    def run_once(self) -> Optional[dict]:
        now = self.clock.now()
        window_ts = window_ts_for(now)
        # if we're already inside the last 10s, roll to the next window
        if now >= window_ts + WINDOW_SECONDS - ENTRY_OFFSET:
            window_ts += WINDOW_SECONDS
        slug = slug_for(window_ts)

        try:
            market = self.data.get_gamma_market(slug)
        except Exception as exc:
            LOG.warning("gamma market lookup failed: %s", exc)
            market = None
        if not market:
            LOG.warning("No market found for %s; skipping window", slug)
            self._sleep_until(window_close_ts(window_ts) + RESOLUTION_DELAY)
            return None

        window_open = self.determine_price_to_beat(window_ts)
        if window_open is None:
            LOG.warning("Could not determine price-to-beat for %s; skipping", slug)
            self._sleep_until(window_close_ts(window_ts) + RESOLUTION_DELAY)
            return None

        LOG.info("Window %s open=%.2f slug=%s", window_ts, window_open, slug)

        fired = self.run_ta_loop(window_ts, window_open)
        signal = fired["signal"]
        side = signal["side"]
        confidence = signal["confidence"]
        score = signal["score"]
        fire_price = fired["price"]

        stake = mode_stake(self.mode, self.state.bankroll,
                           self.state.starting_bankroll, self.min_bet)
        delta_pct = abs((fire_price - window_open) / window_open * 100.0) \
            if window_open else 0.0

        token_id = market["up_token"] if side == "UP" else market["down_token"]

        if self.dry_run:
            entry_price = self.strategy.estimate_token_price(delta_pct)
            shares = stake / entry_price if entry_price else 0.0
            LOG.info("[DRY] %s stake=%.4f entry=%.3f shares=%.4f (%s)",
                     side, stake, entry_price, shares, fired["reason"])
        else:
            order = self.execute_live_order(token_id, stake, window_ts)
            if not order["filled"]:
                LOG.warning("Order not filled for %s; no position", slug)
                shares = 0.0
                entry_price = order["entry_price"]
            else:
                shares = order["shares"]
                entry_price = order["entry_price"]

        # wait for resolution
        self._sleep_until(window_close_ts(window_ts) + RESOLUTION_DELAY)
        resolution = self.resolve(window_ts, window_open, slug)

        if not resolution["resolved"] or shares <= 0:
            outcome = "unresolved" if not resolution["resolved"] else "no_fill"
            pnl = 0.0
        else:
            outcome, pnl = compute_pnl(side, resolution["winner"], entry_price,
                                       shares)

        # update state
        self.state.bankroll += pnl
        self.state.trades += 1
        if outcome == "win":
            self.state.wins += 1

        # bankroll floor handling
        if self.state.bankroll < self.min_bet:
            if self.dry_run:
                LOG.warning("Bankroll %.4f < MIN_BET; resetting to %.4f (dry-run bust)",
                            self.state.bankroll, self.state.starting_bankroll)
                self.state.bankroll = self.state.starting_bankroll
            else:
                LOG.error("Bankroll %.4f < MIN_BET; stopping (live)",
                          self.state.bankroll)
                self._stop = True

        record = {
            "ts": int(self.clock.now()),
            "slug": slug,
            "mode": self.mode,
            "side": side,
            "confidence": confidence,
            "score": score,
            "entry_price": entry_price,
            "shares": shares,
            "stake": stake,
            "dry_run": self.dry_run,
            "outcome": outcome,
            "pnl": pnl,
            "bankroll_after": self.state.bankroll,
        }
        self._persist(record)
        LOG.info("Result %s pnl=%.4f bankroll=%.4f (%s)", outcome, pnl,
                 self.state.bankroll, resolution["source"])
        return record

    def _persist(self, record: dict) -> None:
        os.makedirs(self.runtime_dir, exist_ok=True)
        self.state.save(self.state_path)
        with open(self.trades_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")

    # -- main loop -------------------------------------------------------- #
    def run(self, once: bool = False, max_trades: int = 0) -> None:
        LOG.info("Starting bot mode=%s dry_run=%s min_conf=%.2f bankroll=%.4f",
                 self.mode, self.dry_run, self.min_confidence, self.state.bankroll)
        while not self._stop:
            try:
                self.run_once()
            except Exception as exc:  # never crash the loop
                LOG.exception("Cycle error (continuing): %s", exc)
                self.clock.sleep(5)
            if once:
                break
            if max_trades and self.state.trades >= max_trades:
                LOG.info("Reached max trades (%d); stopping", max_trades)
                break
        LOG.info("Bot stopped. trades=%d wins=%d bankroll=%.4f",
                 self.state.trades, self.state.wins, self.state.bankroll)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    load_dotenv()
    default_mode = os.environ.get("BOT_MODE", "safe")
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5-minute Up/Down trading bot")
    parser.add_argument("--mode", choices=["safe", "aggressive", "degen"],
                        default=default_mode,
                        help="bankroll mode (default from BOT_MODE env or 'safe')")
    parser.add_argument("--dry-run", action="store_true",
                        help="simulate trades; never place real orders")
    parser.add_argument("--once", action="store_true",
                        help="run a single window cycle then exit")
    parser.add_argument("--max-trades", type=int, default=0,
                        help="stop after N trades (0 = unlimited)")
    parser.add_argument("--min-confidence", type=float, default=None,
                        help="override the mode's minimum confidence")
    return parser.parse_args(argv)


def build_bot(args: argparse.Namespace) -> TradingBot:
    starting_bankroll = float(os.environ.get("STARTING_BANKROLL", "1.0"))
    min_bet = float(os.environ.get("MIN_BET", "1.0"))

    data = LiveMarketData()
    feed = ChainlinkPriceFeed()
    feed.start()

    executor = None
    if not args.dry_run:
        try:
            executor = PolymarketExecutor.from_env()
        except Exception as exc:
            LOG.error("Failed to init CLOB executor: %s", exc)
            raise

    return TradingBot(
        mode=args.mode,
        dry_run=args.dry_run,
        data=data,
        executor=executor,
        feed=feed,
        starting_bankroll=starting_bankroll,
        min_bet=min_bet,
        min_confidence=args.min_confidence,
    )


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)
    bot = build_bot(args)

    def _handle_signal(signum, _frame):
        LOG.info("Signal %s received; shutting down gracefully", signum)
        bot.request_stop()
        if bot.feed is not None:
            bot.feed.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        bot.run(once=args.once, max_trades=args.max_trades)
    finally:
        if bot.feed is not None:
            bot.feed.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
