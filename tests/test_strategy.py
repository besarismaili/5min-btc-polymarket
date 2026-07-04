"""Offline unit tests for strategy.py (no network)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy  # noqa: E402


def candle(ts, o, h, l, c, v):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def flat_candles(n, price=100.0, vol=10.0):
    """n identical doji candles -> every indicator contributes 0 except window
    delta (and EMA/RSI which return 0 for short lists when n < their period)."""
    return [candle(i * 60_000, price, price, price, price, vol) for i in range(n)]


# --- Indicator 1: window delta buckets -------------------------------------

@pytest.mark.parametrize("current, expected", [
    (100.11, 7.0),    # >0.10%
    (100.05, 5.0),    # >0.02%
    (100.011, 3.0),   # >0.005%
    (100.003, 1.0),   # >0.001%
    (100.0005, 0.0),  # <=0.001%
    (99.89, -7.0),    # negative large
    (99.95, -5.0),
])
def test_window_delta(current, expected):
    candles = flat_candles(6)  # keeps other indicators at 0
    result = strategy.analyze(candles, window_open=100.0, current_price=current)
    assert result["components"]["window_delta"] == expected


# --- Indicator 2: micro momentum -------------------------------------------

def test_micro_momentum_both_up():
    # candles[-3] and candles[-2] must both close up.
    candles = [
        candle(0, 100, 100, 100, 100, 10),
        candle(60_000, 100, 101, 100, 101, 10),   # index -3 up
        candle(120_000, 101, 102, 101, 102, 10),  # index -2 up
        candle(180_000, 102, 102, 102, 102, 10),  # index -1 in progress
    ]
    r = strategy.analyze(candles, 100.0, 100.0)
    assert r["components"]["micro_momentum"] == 2.0


def test_micro_momentum_both_down():
    candles = [
        candle(0, 100, 100, 100, 100, 10),
        candle(60_000, 101, 101, 100, 100, 10),   # down
        candle(120_000, 100, 100, 99, 99, 10),    # down
        candle(180_000, 99, 99, 99, 99, 10),
    ]
    r = strategy.analyze(candles, 100.0, 100.0)
    assert r["components"]["micro_momentum"] == -2.0


def test_micro_momentum_mixed():
    candles = [
        candle(0, 100, 100, 100, 100, 10),
        candle(60_000, 100, 101, 100, 101, 10),   # up
        candle(120_000, 101, 101, 100, 100, 10),  # down
        candle(180_000, 100, 100, 100, 100, 10),
    ]
    r = strategy.analyze(candles, 100.0, 100.0)
    assert r["components"]["micro_momentum"] == 0.0


# --- Indicator 3: acceleration ---------------------------------------------

def test_acceleration_growing_up():
    candles = [
        candle(0, 100, 100, 100, 100, 10),
        candle(60_000, 100, 100, 100, 100.5, 10),   # index -4: small up body 0.5
        candle(120_000, 100, 100, 100, 100, 10),
        candle(180_000, 100, 100, 100, 102, 10),     # index -2: big up body 2.0
        candle(240_000, 102, 102, 102, 102, 10),     # in progress
    ]
    r = strategy.analyze(candles, 100.0, 100.0)
    assert r["components"]["acceleration"] == 1.5


def test_acceleration_fading_up():
    candles = [
        candle(0, 100, 100, 100, 100, 10),
        candle(60_000, 100, 100, 100, 103, 10),      # index -4: big body 3.0
        candle(120_000, 103, 103, 103, 103, 10),
        candle(180_000, 103, 103, 103, 103.5, 10),   # index -2: small body 0.5
        candle(240_000, 103.5, 103.5, 103.5, 103.5, 10),
    ]
    r = strategy.analyze(candles, 100.0, 100.0)
    assert r["components"]["acceleration"] == -1.5


# --- Indicator 4: EMA 9/21 -------------------------------------------------

def test_ema_uptrend():
    closes = [100 + i for i in range(30)]  # strictly rising
    candles = [candle(i * 60_000, c, c, c, c, 10) for i, c in enumerate(closes)]
    r = strategy.analyze(candles, closes[0], closes[-1])
    assert r["components"]["ema"] == 1.0


def test_ema_downtrend():
    closes = [130 - i for i in range(30)]
    candles = [candle(i * 60_000, c, c, c, c, 10) for i, c in enumerate(closes)]
    r = strategy.analyze(candles, closes[0], closes[-1])
    assert r["components"]["ema"] == -1.0


def test_ema_short_list_zero():
    candles = flat_candles(5)
    r = strategy.analyze(candles, 100.0, 100.0)
    assert r["components"]["ema"] == 0.0


# --- Indicator 5: RSI 14 (momentum following) ------------------------------

def test_rsi_strong_up():
    closes = [100 + i for i in range(20)]  # all gains -> RSI ~100
    candles = [candle(i * 60_000, c, c, c, c, 10) for i, c in enumerate(closes)]
    r = strategy.analyze(candles, closes[0], closes[-1])
    assert r["components"]["rsi"] == 2.0


def test_rsi_strong_down():
    closes = [130 - i for i in range(20)]
    candles = [candle(i * 60_000, c, c, c, c, 10) for i, c in enumerate(closes)]
    r = strategy.analyze(candles, closes[0], closes[-1])
    assert r["components"]["rsi"] == -2.0


# --- Indicator 6: volume surge ---------------------------------------------

def test_volume_surge_up():
    # prior 3 low volume, recent 3 high volume, positive delta.
    vols = [10, 10, 10, 30, 30, 30]
    candles = [candle(i * 60_000, 100, 100, 100, 100, v) for i, v in enumerate(vols)]
    r = strategy.analyze(candles, 100.0, 100.2)  # positive delta
    assert r["components"]["volume_surge"] == 1.0


def test_volume_surge_none_when_no_delta():
    vols = [10, 10, 10, 30, 30, 30]
    candles = [candle(i * 60_000, 100, 100, 100, 100, v) for i, v in enumerate(vols)]
    r = strategy.analyze(candles, 100.0, 100.0)  # zero delta
    assert r["components"]["volume_surge"] == 0.0


def test_volume_surge_down_direction():
    vols = [10, 10, 10, 30, 30, 30]
    candles = [candle(i * 60_000, 100, 100, 100, 100, v) for i, v in enumerate(vols)]
    r = strategy.analyze(candles, 100.0, 99.8)  # negative delta
    assert r["components"]["volume_surge"] == -1.0


# --- Indicator 7: tick trend -----------------------------------------------

def test_tick_trend_up():
    ticks = [100.0, 100.02, 100.04, 100.06, 100.08]  # all up, > 0.005%
    r = strategy.analyze(flat_candles(6), 100.0, 100.0, ticks=ticks)
    assert r["components"]["tick_trend"] == 2.0


def test_tick_trend_down():
    ticks = [100.0, 99.98, 99.96, 99.94, 99.92]
    r = strategy.analyze(flat_candles(6), 100.0, 100.0, ticks=ticks)
    assert r["components"]["tick_trend"] == -2.0


def test_tick_trend_flat_zero():
    ticks = [100.0, 100.0, 100.0]
    r = strategy.analyze(flat_candles(6), 100.0, 100.0, ticks=ticks)
    assert r["components"]["tick_trend"] == 0.0


def test_tick_trend_none():
    r = strategy.analyze(flat_candles(6), 100.0, 100.0, ticks=None)
    assert r["components"]["tick_trend"] == 0.0


# --- Composite / confidence / side -----------------------------------------

def test_composite_score_and_confidence():
    r = strategy.analyze(flat_candles(6), 100.0, 100.11)  # window_delta only = +7
    assert r["score"] == 7.0
    assert r["confidence"] == 1.0
    assert r["side"] == "UP"


def test_composite_down_side():
    r = strategy.analyze(flat_candles(6), 100.0, 99.89)  # -7
    assert r["score"] == -7.0
    assert r["side"] == "DOWN"
    assert r["confidence"] == 1.0


def test_composite_zero_is_up_zero_confidence():
    r = strategy.analyze(flat_candles(6), 100.0, 100.0)
    assert r["score"] == 0.0
    assert r["confidence"] == 0.0
    assert r["side"] == "UP"


def test_score_is_sum_of_components():
    r = strategy.analyze(flat_candles(6), 100.0, 100.05)
    assert r["score"] == pytest.approx(sum(r["components"].values()))


def test_confidence_capped_at_one():
    # Stack multiple indicators; confidence must never exceed 1.0.
    closes = [100 + i for i in range(30)]
    candles = [candle(i * 60_000, c, c, c, c, 10) for i, c in enumerate(closes)]
    r = strategy.analyze(candles, closes[0], closes[0] * 1.02)
    assert 0.0 <= r["confidence"] <= 1.0


def test_empty_candles_graceful():
    r = strategy.analyze([], 100.0, 100.0)
    assert r["side"] == "UP"
    assert r["confidence"] == 0.0
    assert all(v == 0.0 for v in r["components"].values())


# --- Token pricing model ---------------------------------------------------

def test_token_price_control_points():
    assert strategy.estimate_token_price(0.0) == pytest.approx(0.50)
    assert strategy.estimate_token_price(0.005) == pytest.approx(0.50)
    assert strategy.estimate_token_price(0.02) == pytest.approx(0.55)
    assert strategy.estimate_token_price(0.05) == pytest.approx(0.65)
    assert strategy.estimate_token_price(0.10) == pytest.approx(0.80)
    assert strategy.estimate_token_price(0.15) == pytest.approx(0.92)
    assert strategy.estimate_token_price(0.25) == pytest.approx(0.97)


def test_token_price_bounds():
    assert strategy.estimate_token_price(0.0) >= 0.50
    assert strategy.estimate_token_price(5.0) <= 0.97
    assert strategy.estimate_token_price(1.0) == pytest.approx(0.97)


def test_token_price_uses_abs():
    assert strategy.estimate_token_price(-0.10) == pytest.approx(0.80)


def test_token_price_monotonic_non_decreasing():
    xs = [i * 0.001 for i in range(0, 300)]
    prices = [strategy.estimate_token_price(x) for x in xs]
    for a, b in zip(prices, prices[1:]):
        assert b >= a - 1e-12


def test_token_price_interpolates_midpoint():
    # Between 0.02 (0.55) and 0.05 (0.65); midpoint delta 0.035 -> 0.60.
    assert strategy.estimate_token_price(0.035) == pytest.approx(0.60)
