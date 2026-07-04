"""Offline tests for bot.py.

Network is blocked in this sandbox, so every test uses injected fakes: a
``FakeClock`` that advances virtual time on ``sleep``, a stub market-data
provider, a fake strategy module and (where needed) a seeded price feed. No
socket or HTTP call is ever made.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as botmod
from bot import (
    BotState,
    ChainlinkPriceFeed,
    TradingBot,
    compute_pnl,
    evaluate_fire,
    mode_min_confidence,
    mode_stake,
    resolve_outcome,
    slug_for,
    window_close_ts,
    window_ts_for,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self, start):
        self.t = float(start)

    def now(self):
        return self.t

    def sleep(self, seconds):
        if seconds > 0:
            self.t += seconds


class FakeStrategy:
    """Configurable stand-in for the real strategy module."""

    def __init__(self, score=1.0, confidence=0.1, side="UP", entry_price=0.5):
        self._score = score
        self._confidence = confidence
        self._side = side
        self._entry_price = entry_price
        self.analyze_calls = 0

    def analyze(self, candles, window_open, current_price, ticks=None):
        self.analyze_calls += 1
        return {
            "score": self._score,
            "confidence": self._confidence,
            "side": self._side,
            "components": {},
        }

    def estimate_token_price(self, delta_pct):
        return self._entry_price


class StubData:
    """Scripted market-data provider."""

    def __init__(self, window_open=50000.0, close_price=50100.0,
                 ticker=50100.0, market=None):
        self._window_open = window_open
        self._close_price = close_price
        self._ticker = ticker
        self._market = market or {
            "slug": "btc-updown-5m-x",
            "up_token": "UP_TOKEN",
            "down_token": "DOWN_TOKEN",
            "outcomes": ["Up", "Down"],
            "outcome_prices": {"Up": 0.5, "Down": 0.5},
            "end_date": None,
            "raw": {},
        }
        self.gamma_prices = None  # set to dict to force gamma fallback

    def get_gamma_market(self, slug):
        return self._market

    def get_candles(self, limit=30):
        return [{"ts": i * 60000, "open": 50000, "high": 50100,
                 "low": 49900, "close": 50050, "volume": 1.0}
                for i in range(limit)]

    def get_ticker_price(self):
        return self._ticker

    def get_price_to_beat(self, window_ts):
        return self._window_open

    def get_close_price(self, window_ts):
        return self._close_price

    def get_gamma_outcome_prices(self, slug):
        return self.gamma_prices


def make_bot(tmp_path, clock, data=None, strategy=None, mode="safe",
             dry_run=True, starting_bankroll=100.0, min_bet=1.0,
             feed=None, min_confidence=None):
    return TradingBot(
        mode=mode,
        dry_run=dry_run,
        data=data or StubData(),
        strategy=strategy or FakeStrategy(),
        clock=clock,
        feed=feed,
        starting_bankroll=starting_bankroll,
        min_bet=min_bet,
        min_confidence=min_confidence,
        runtime_dir=str(tmp_path / "runtime"),
    )


# --------------------------------------------------------------------------- #
# Window / slug math
# --------------------------------------------------------------------------- #
def test_window_ts_floors_to_5min():
    assert window_ts_for(1_000_000_000) == 1_000_000_000 - (1_000_000_000 % 300)
    # exactly on a boundary stays put
    assert window_ts_for(1_000_000_200) % 300 == 0
    # 1 second before boundary rolls back to previous open
    boundary = 1_000_000_200
    assert window_ts_for(boundary - 1) == boundary - 300


def test_window_close_and_slug():
    wt = window_ts_for(1_700_000_123)
    assert window_close_ts(wt) == wt + 300
    assert slug_for(wt) == f"btc-updown-5m-{wt}"


# --------------------------------------------------------------------------- #
# Mode sizing (incl. floors)
# --------------------------------------------------------------------------- #
def test_mode_min_confidence():
    assert mode_min_confidence("safe") == 0.30
    assert mode_min_confidence("aggressive") == 0.20
    assert mode_min_confidence("degen") == 0.0


def test_safe_stake_is_quarter_with_floor():
    assert mode_stake("safe", bankroll=100.0, starting_bankroll=100.0,
                      min_bet=1.0) == 25.0
    # floor: 25% of 2 = 0.5 -> floored to min_bet 1.0
    assert mode_stake("safe", bankroll=2.0, starting_bankroll=2.0,
                      min_bet=1.0) == 1.0


def test_aggressive_bets_profit_only_with_floor():
    # profit = 150 - 100 = 50
    assert mode_stake("aggressive", bankroll=150.0, starting_bankroll=100.0,
                      min_bet=1.0) == 50.0
    # no profit -> floor to min_bet
    assert mode_stake("aggressive", bankroll=90.0, starting_bankroll=100.0,
                      min_bet=1.0) == 1.0


def test_degen_bets_full_bankroll():
    assert mode_stake("degen", bankroll=42.0, starting_bankroll=100.0,
                      min_bet=1.0) == 42.0


def test_stake_never_exceeds_bankroll():
    # min_bet larger than bankroll must not exceed available funds
    assert mode_stake("safe", bankroll=0.5, starting_bankroll=100.0,
                      min_bet=1.0) == 0.5


# --------------------------------------------------------------------------- #
# Fire decision logic
# --------------------------------------------------------------------------- #
def _sig(score, conf, side="UP"):
    return {"score": score, "confidence": conf, "side": side, "components": {}}


def test_fire_at_deadline_always():
    fire, reason = evaluate_fire(prev_score=0.0, signal=_sig(0.1, 0.0),
                                 min_conf=0.9, at_deadline=True)
    assert fire and reason == "deadline"


def test_fire_on_score_spike():
    fire, reason = evaluate_fire(prev_score=0.0, signal=_sig(1.5, 0.05),
                                 min_conf=0.9, at_deadline=False)
    assert fire and reason == "spike"


def test_no_fire_below_spike_and_confidence():
    fire, reason = evaluate_fire(prev_score=0.0, signal=_sig(1.0, 0.1),
                                 min_conf=0.9, at_deadline=False)
    assert not fire and reason is None


def test_fire_on_confidence():
    fire, reason = evaluate_fire(prev_score=1.0, signal=_sig(1.2, 0.5),
                                 min_conf=0.30, at_deadline=False)
    assert fire and reason == "confidence"


# --------------------------------------------------------------------------- #
# Resolution + pnl helpers
# --------------------------------------------------------------------------- #
def test_resolve_outcome():
    assert resolve_outcome(100.0, 101.0) == "UP"
    assert resolve_outcome(100.0, 100.0) == "UP"   # tie -> up_wins
    assert resolve_outcome(100.0, 99.0) == "DOWN"


def test_compute_pnl_win_and_loss():
    outcome, pnl = compute_pnl("UP", "UP", entry_price=0.5, shares=50.0)
    assert outcome == "win" and pnl == pytest.approx(25.0)
    outcome, pnl = compute_pnl("UP", "DOWN", entry_price=0.5, shares=50.0)
    assert outcome == "loss" and pnl == pytest.approx(-25.0)


# --------------------------------------------------------------------------- #
# Chainlink feed data access (offline, no socket)
# --------------------------------------------------------------------------- #
def test_price_feed_ring_buffer_and_price_at():
    feed = ChainlinkPriceFeed(buffer_size=4)
    feed.record_tick(1000, 100.0)
    feed.record_tick(2000, 101.0)
    feed.record_tick(3000, 102.0)
    assert feed.latest() == 102.0
    # nearest within tolerance
    assert feed.price_at(2100, tolerance_ms=500) == 101.0
    # nothing within tolerance
    assert feed.price_at(50_000, tolerance_ms=100) is None
    # ring buffer bounded
    feed.record_tick(4000, 103.0)
    feed.record_tick(5000, 104.0)
    assert feed.recent_values(10) == [101.0, 102.0, 103.0, 104.0]


# --------------------------------------------------------------------------- #
# State persistence round-trip
# --------------------------------------------------------------------------- #
def test_bot_state_round_trip(tmp_path):
    path = str(tmp_path / "runtime" / "bot_state.json")
    state = BotState(bankroll=123.45, starting_bankroll=100.0, trades=7, wins=4)
    state.save(path)
    loaded = BotState.load(path, starting_bankroll=100.0)
    assert loaded.to_dict() == state.to_dict()


def test_bot_state_load_missing_returns_defaults(tmp_path):
    path = str(tmp_path / "runtime" / "missing.json")
    loaded = BotState.load(path, starting_bankroll=55.0)
    assert loaded.bankroll == 55.0 and loaded.starting_bankroll == 55.0
    assert loaded.trades == 0 and loaded.wins == 0


# --------------------------------------------------------------------------- #
# Full dry-run trade cycle end-to-end
# --------------------------------------------------------------------------- #
def test_dry_run_cycle_win(tmp_path):
    # window open exactly on a 5-min boundary; start 20s in (before T-10s)
    base = window_ts_for(1_700_000_000)
    clock = FakeClock(base + 280)   # T-20s -> before entry (T-10s = base+290)
    data = StubData(window_open=50000.0, close_price=50100.0, ticker=50100.0)
    strat = FakeStrategy(score=1.0, confidence=0.1, side="UP", entry_price=0.5)
    bot = make_bot(tmp_path, clock, data=data, strategy=strat, mode="safe",
                   starting_bankroll=100.0, min_bet=1.0)

    record = bot.run_once()

    assert record is not None
    assert record["side"] == "UP"
    assert record["dry_run"] is True
    assert record["outcome"] == "win"
    # safe stake = 25; entry 0.5 -> 50 shares; win pnl = 50*(1-0.5) = 25
    assert record["stake"] == pytest.approx(25.0)
    assert record["shares"] == pytest.approx(50.0)
    assert record["pnl"] == pytest.approx(25.0)
    assert record["bankroll_after"] == pytest.approx(125.0)
    # the TA loop actually polled several times before the deadline
    assert strat.analyze_calls >= 2

    # state + trade log persisted
    state = BotState.load(str(tmp_path / "runtime" / "bot_state.json"), 100.0)
    assert state.bankroll == pytest.approx(125.0)
    assert state.trades == 1 and state.wins == 1
    lines = (tmp_path / "runtime" / "trades.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["outcome"] == "win"


def test_dry_run_cycle_loss_updates_bankroll(tmp_path):
    base = window_ts_for(1_700_000_000)
    clock = FakeClock(base + 280)
    # price falls -> DOWN wins, but strategy says UP -> loss
    data = StubData(window_open=50000.0, close_price=49900.0, ticker=49900.0)
    strat = FakeStrategy(score=1.0, confidence=0.9, side="UP", entry_price=0.5)
    bot = make_bot(tmp_path, clock, data=data, strategy=strat, mode="safe",
                   starting_bankroll=100.0, min_bet=1.0)

    record = bot.run_once()
    assert record["outcome"] == "loss"
    assert record["pnl"] == pytest.approx(-25.0)
    assert record["bankroll_after"] == pytest.approx(75.0)


def test_dry_run_bankroll_floor_resets(tmp_path):
    base = window_ts_for(1_700_000_000)
    clock = FakeClock(base + 280)
    # start near broke; a loss pushes below MIN_BET -> dry-run reset to start
    data = StubData(window_open=50000.0, close_price=49900.0, ticker=49900.0)
    strat = FakeStrategy(score=1.0, confidence=0.9, side="UP", entry_price=0.5)
    bot = make_bot(tmp_path, clock, data=data, strategy=strat, mode="degen",
                   starting_bankroll=10.0, min_bet=1.0)
    bot.state.bankroll = 1.5  # degen bets full 1.5 -> 3 shares -> loss -1.5 -> 0

    record = bot.run_once()
    assert record["outcome"] == "loss"
    # bankroll went to ~0 (< MIN_BET) -> reset to starting 10.0
    assert bot.state.bankroll == pytest.approx(10.0)


def test_gamma_fallback_resolution(tmp_path):
    base = window_ts_for(1_700_000_000)
    clock = FakeClock(base + 280)
    data = StubData(window_open=50000.0, ticker=50100.0)
    # no price close available -> force gamma fallback
    data.get_close_price = lambda window_ts: None
    data.gamma_prices = {"Up": 0.995, "Down": 0.005}
    strat = FakeStrategy(score=1.0, confidence=0.1, side="UP", entry_price=0.5)
    bot = make_bot(tmp_path, clock, data=data, strategy=strat)

    record = bot.run_once()
    assert record["outcome"] == "win"


def test_deadline_fire_uses_best_signal(tmp_path):
    # confidence stays below min_conf so it should fire at the deadline
    base = window_ts_for(1_700_000_000)
    clock = FakeClock(base + 288)
    data = StubData(window_open=50000.0, close_price=50100.0, ticker=50100.0)
    strat = FakeStrategy(score=0.5, confidence=0.05, side="UP", entry_price=0.5)
    bot = make_bot(tmp_path, clock, data=data, strategy=strat, mode="safe")

    record = bot.run_once()
    # never skipped despite low confidence
    assert record is not None and record["side"] == "UP"


def test_feed_price_to_beat_preferred(tmp_path):
    base = window_ts_for(1_700_000_000)
    clock = FakeClock(base + 280)
    feed = ChainlinkPriceFeed()
    feed.record_tick(base * 1000, 60000.0)  # chainlink open at boundary
    data = StubData(window_open=50000.0, close_price=60100.0, ticker=60100.0)
    strat = FakeStrategy(score=1.0, confidence=0.1, side="UP", entry_price=0.5)
    bot = make_bot(tmp_path, clock, data=data, strategy=strat, feed=feed)

    # price-to-beat should come from the feed (60000), not binance (50000)
    assert bot.determine_price_to_beat(base) == 60000.0


def test_min_confidence_override():
    bot = TradingBot(mode="safe", min_confidence=0.75, dry_run=True,
                     data=StubData(), strategy=FakeStrategy())
    assert bot.min_confidence == 0.75
    bot2 = TradingBot(mode="safe", dry_run=True, data=StubData(),
                      strategy=FakeStrategy())
    assert bot2.min_confidence == 0.30


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("BOT_MODE", raising=False)
    args = botmod.parse_args([])
    assert args.mode == "safe"
    assert args.dry_run is False
    assert args.max_trades == 0
    assert args.min_confidence is None


def test_parse_args_custom():
    args = botmod.parse_args(["--mode", "degen", "--dry-run", "--once",
                              "--max-trades", "5", "--min-confidence", "0.4"])
    assert args.mode == "degen"
    assert args.dry_run is True
    assert args.once is True
    assert args.max_trades == 5
    assert args.min_confidence == 0.4
