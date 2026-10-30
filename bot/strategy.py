"""Technical indicators and signal generation.

Deliberately implemented with plain Python lists instead of pandas/NumPy:
the arrays here are a few hundred candles long, and avoiding the heavy
dependency keeps the install reliable on a fresh machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


def ema(closes: list[float], period: int) -> list[float]:
    """Exponential moving average, seeded with the SMA of the first `period`.

    Returns a list whose last element is aligned with `closes[-1]`.
    Returns an empty list when there is not enough data.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period:
        return []

    multiplier = 2.0 / (period + 1.0)
    out = [sum(closes[:period]) / period]
    for price in closes[period:]:
        out.append(price * multiplier + out[-1] * (1.0 - multiplier))
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Relative Strength Index using Wilder's smoothing.

    The first value is a plain average of the initial window; subsequent
    values apply Wilder's recursive smoothing.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return []

    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [_rsi_value(avg_gain, avg_loss)]

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(_rsi_value(avg_gain, avg_loss))

    return out


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Everything the strategy decided about the latest closed candle."""

    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    crossed_up: bool
    crossed_down: bool

    def describe(self) -> str:
        trend = "up" if self.ema_fast > self.ema_slow else "down"
        cross = ""
        if self.crossed_up:
            cross = " [CROSS UP]"
        elif self.crossed_down:
            cross = " [CROSS DOWN]"
        return (
            f"close={self.close:.6g} "
            f"ema{self.ema_fast:.6g} ema{self.ema_slow:.6g} ({trend}) "
            f"rsi={self.rsi:.1f}{cross}"
        )


def compute_indicators(closes: list[float], cfg) -> IndicatorSnapshot:
    """Compute an aligned snapshot for the most recent closed candle.

    Raises ValueError if there is not enough history to be meaningful.
    """
    # Enough candles for BOTH indicators to warm up, plus one more so a
    # crossover can actually be observed. RSI's warm-up dominates whenever
    # the EMA settings are faster than the RSI period.
    required = max(cfg.ema_slow + 2, cfg.rsi_period + 1)
    if len(closes) < required:
        raise ValueError(
            f"Need at least {required} candles for EMA {cfg.ema_fast}/{cfg.ema_slow}, "
            f"got {len(closes)}"
        )

    fast_series = ema(closes, cfg.ema_fast)
    slow_series = ema(closes, cfg.ema_slow)
    rsi_series = rsi(closes, cfg.rsi_period)
    if not rsi_series:
        raise ValueError(f"Need more history for RSI period {cfg.rsi_period}")

    # Both EMA series end at the same candle; the slow one is shorter, so
    # aligning on its length lines them up correctly.
    n = min(len(fast_series), len(slow_series))
    fast_series = fast_series[-n:]
    slow_series = slow_series[-n:]

    if n < 2:
        raise ValueError("Not enough aligned EMA data to detect a crossover.")

    crossed_up = fast_series[-2] <= slow_series[-2] and fast_series[-1] > slow_series[-1]
    crossed_down = fast_series[-2] >= slow_series[-2] and fast_series[-1] < slow_series[-1]

    return IndicatorSnapshot(
        close=closes[-1],
        ema_fast=fast_series[-1],
        ema_slow=slow_series[-1],
        rsi=rsi_series[-1],
        crossed_up=crossed_up,
        crossed_down=crossed_down,
    )


def indicator_series(closes: list[float], cfg) -> dict:
    """EMA and RSI series aligned to `closes`, for drawing on the chart.

    Each series is shorter than the candle list by its own warm-up period,
    so the start index is returned alongside it. The dashboard maps
    `start + i` back onto the candle's timestamp.

    Pure and network-free, like the rest of this module, so it stays usable
    from a backtester.
    """
    n = len(closes)
    fast = ema(closes, cfg.ema_fast)
    slow = ema(closes, cfg.ema_slow)
    rsi_values = rsi(closes, cfg.rsi_period)

    def _pack(series: list[float]) -> dict:
        return {"start": n - len(series), "values": series}

    return {
        "emaFast": _pack(fast),
        "emaSlow": _pack(slow),
        "rsi": _pack(rsi_values),
    }


def evaluate(closes: list[float], cfg) -> tuple[Signal, IndicatorSnapshot]:
    """Turn a candle history into a trade signal.

    This is a trend-following strategy:
      * BUY  on an upward EMA crossover, but only when RSI is not
        overextended (below `rsi_max_entry`) and not in free-fall
        (above `rsi_min_entry`).
      * SELL on a downward EMA crossover.
      * HOLD otherwise.

    Take-profit / stop-loss are handled separately by the risk manager,
    since they depend on the open position rather than on price history.

    NOTE on `rsi_min_entry`: the falling-knife guard is only reachable with
    fast EMA settings (e.g. 5/8). With the defaults (9/21) a cross-up
    structurally requires enough sustained upside that RSI at the cross
    never drops below ~37, so this guard will not fire. It is cheap and
    harmless to keep, but do not rely on it as a safety net at 9/21 --
    STOP_LOSS is what actually limits downside.
    """
    snapshot = compute_indicators(closes, cfg)

    if snapshot.crossed_up:
        if cfg.rsi_min_entry <= snapshot.rsi <= cfg.rsi_max_entry:
            return Signal.BUY, snapshot
        # A valid cross that is filtered out by RSI is still a HOLD, not a SELL.
        return Signal.HOLD, snapshot

    if snapshot.crossed_down:
        return Signal.SELL, snapshot

    return Signal.HOLD, snapshot
