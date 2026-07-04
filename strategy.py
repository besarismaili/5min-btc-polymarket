"""Signal engine for the Polymarket BTC 5-minute up/down bot.

This module implements a 7-indicator weighted signal engine. Positive
contributions vote UP, negative contributions vote DOWN. It also provides a
delta-based token pricing model used by dry-runs and backtests to estimate the
entry price of the "correct" side without hitting the live order book.

Candle format (shared across modules): a ``dict`` with keys
``ts`` (int, ms epoch of candle open), ``open``, ``high``, ``low``, ``close``,
``volume`` (floats). Candles are 1-minute BTCUSDT candles ending at "now"; the
last candle may still be in-progress.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Helper indicators
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> Optional[float]:
    """Standard exponential moving average of ``values``.

    Returns ``None`` if there is not enough data (fewer than ``period`` values).
    """
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    # Seed with the simple moving average of the first ``period`` values.
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI over ``closes``. Returns ``None`` without enough data."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # Initial average over the first ``period`` deltas.
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing for the rest.
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Individual indicator contributions
# ---------------------------------------------------------------------------

def _window_delta_contribution(delta_pct: float) -> float:
    """Indicator 1 (dominant): magnitude buckets on the window delta percent."""
    mag = abs(delta_pct)
    sgn = _sign(delta_pct)
    if mag > 0.10:
        c = 7.0
    elif mag > 0.02:
        c = 5.0
    elif mag > 0.005:
        c = 3.0
    elif mag > 0.001:
        c = 1.0
    else:
        c = 0.0
    return c * sgn


def _micro_momentum_contribution(candles: list[dict]) -> float:
    """Indicator 2 (weight 2): direction of the last two *closed* candles.

    A candle counts as in-progress if it is the final element; we therefore use
    candles[-3] and candles[-2] as the two most recent closed candles when the
    last candle is in-progress. We only need close vs open per candle.
    """
    if len(candles) < 3:
        return 0.0
    c1 = candles[-3]
    c2 = candles[-2]
    d1 = _sign(c1["close"] - c1["open"])
    d2 = _sign(c2["close"] - c2["open"])
    if d1 > 0 and d2 > 0:
        return 2.0
    if d1 < 0 and d2 < 0:
        return -2.0
    return 0.0


def _acceleration_contribution(candles: list[dict]) -> float:
    """Indicator 3 (weight 1.5): is the move growing or fading?

    Compares the body (close-open) of the latest closed candle to the candle two
    back. If the move in the current direction is growing -> sign of current
    direction * 1.5; if fading -> opposite.
    """
    if len(candles) < 4:
        return 0.0
    latest = candles[-2]          # latest closed candle
    prior = candles[-4]           # candle two back from latest closed
    latest_body = latest["close"] - latest["open"]
    prior_body = prior["close"] - prior["open"]
    cur_dir = _sign(latest_body)
    if cur_dir == 0:
        return 0.0
    # Growing if magnitude of latest body exceeds prior body magnitude.
    if abs(latest_body) > abs(prior_body):
        return 1.5 * cur_dir
    return -1.5 * cur_dir


def _ema_contribution(closes: list[float]) -> float:
    """Indicator 4 (weight 1): EMA9 vs EMA21 on closes."""
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    if ema9 is None or ema21 is None:
        return 0.0
    return 1.0 if ema9 > ema21 else -1.0


def _rsi_contribution(closes: list[float]) -> float:
    """Indicator 5 (weight 2): momentum-following RSI extremes."""
    rsi = _rsi(closes, 14)
    if rsi is None:
        return 0.0
    if rsi > 75:
        return 2.0
    if rsi < 25:
        return -2.0
    return 0.0


def _volume_surge_contribution(candles: list[dict], delta_pct: float) -> float:
    """Indicator 6 (weight 1): recent volume surge in the delta direction."""
    if len(candles) < 6:
        return 0.0
    recent = candles[-3:]
    prior = candles[-6:-3]
    recent_avg = sum(c["volume"] for c in recent) / 3.0
    prior_avg = sum(c["volume"] for c in prior) / 3.0
    if prior_avg <= 0:
        return 0.0
    sgn = _sign(delta_pct)
    if sgn == 0:
        return 0.0
    if recent_avg >= 1.5 * prior_avg:
        return 1.0 * sgn
    return 0.0


def _tick_trend_contribution(ticks: Optional[list[float]]) -> float:
    """Indicator 7 (weight 2): real-time tick trend over recent poll prices."""
    if not ticks or len(ticks) < 3:
        return 0.0
    moves = [ticks[i] - ticks[i - 1] for i in range(1, len(ticks))]
    nonzero = [m for m in moves if m != 0]
    if not nonzero:
        return 0.0
    up = sum(1 for m in nonzero if m > 0)
    down = sum(1 for m in nonzero if m < 0)
    total = len(nonzero)
    start = ticks[0]
    if start == 0:
        return 0.0
    total_move_pct = abs(ticks[-1] - ticks[0]) / abs(start) * 100.0
    if total_move_pct < 0.005:
        return 0.0
    if up / total >= 0.60:
        return 2.0
    if down / total >= 0.60:
        return -2.0
    return 0.0


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def analyze(candles: list[dict], window_open: float, current_price: float,
            ticks: Optional[list[float]] = None) -> dict:
    """Compute the composite signal.

    Returns ``{"score": float, "confidence": float, "side": "UP"|"DOWN",
    "components": {name: contribution}}``.
    """
    candles = candles or []
    closes = [c["close"] for c in candles]

    if window_open and window_open != 0:
        delta_pct = (current_price - window_open) / window_open * 100.0
    else:
        delta_pct = 0.0

    components = {
        "window_delta": _window_delta_contribution(delta_pct),
        "micro_momentum": _micro_momentum_contribution(candles),
        "acceleration": _acceleration_contribution(candles),
        "ema": _ema_contribution(closes),
        "rsi": _rsi_contribution(closes),
        "volume_surge": _volume_surge_contribution(candles, delta_pct),
        "tick_trend": _tick_trend_contribution(ticks),
    }

    score = sum(components.values())
    confidence = min(abs(score) / 7.0, 1.0)
    side = "UP" if score >= 0 else "DOWN"

    return {
        "score": score,
        "confidence": confidence,
        "side": side,
        "components": components,
    }


# Piecewise-linear control points for the token pricing model.
_PRICE_POINTS = [
    (0.000, 0.50),
    (0.005, 0.50),
    (0.02, 0.55),
    (0.05, 0.65),
    (0.10, 0.80),
    (0.15, 0.92),
    (0.25, 0.97),
]

_PRICE_FLOOR = 0.50
_PRICE_CAP = 0.97


def estimate_token_price(delta_pct: float) -> float:
    """Delta-based token pricing model for dry-run/backtests.

    ``delta_pct`` is expressed in percent units; its absolute value is used.
    Piecewise-linear through the control points, linearly approaching 0.97 by a
    delta of 0.25, then flat. Never below 0.50 or above 0.97.
    """
    x = abs(delta_pct)
    if x >= _PRICE_POINTS[-1][0]:
        return _PRICE_CAP
    for i in range(1, len(_PRICE_POINTS)):
        x0, y0 = _PRICE_POINTS[i - 1]
        x1, y1 = _PRICE_POINTS[i]
        if x <= x1:
            if x1 == x0:
                price = y1
            else:
                frac = (x - x0) / (x1 - x0)
                price = y0 + frac * (y1 - y0)
            break
    else:  # pragma: no cover - guarded by the >= check above
        price = _PRICE_CAP
    return max(_PRICE_FLOOR, min(_PRICE_CAP, price))
