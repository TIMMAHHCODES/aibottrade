"""Self-test for the indicator math. No network access.

Run:  python test_strategy.py

The RSI expectations here are cross-checked against a from-scratch
reference implementation in `wilder_rsi_ref`, which is deliberately written
differently from the one under test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.strategy import (  # noqa: E402
    Signal,
    ema,
    evaluate,
    indicator_series,
    rsi,
)


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def make_cfg(**overrides) -> SimpleNamespace:
    base = dict(
        ema_fast=9, ema_slow=21, rsi_period=14,
        rsi_max_entry=70.0, rsi_min_entry=20.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def wilder_rsi_ref(closes: list[float], n: int) -> list[float]:
    """Independent Wilder RSI, written to differ structurally from the SUT."""
    gains = [max(closes[i + 1] - closes[i], 0.0) for i in range(len(closes) - 1)]
    losses = [max(closes[i] - closes[i + 1], 0.0) for i in range(len(closes) - 1)]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out = [100.0 if al == 0 else 100 - 100 / (1 + ag / al)]
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def find_cross(closes: list[float], cfg, direction: str) -> int:
    """Return the shortest prefix length that produces the requested cross.

    Building a synthetic series that crosses on *exactly* the last candle is
    fiddly; searching for where the cross actually lands is robust.
    """
    for n in range(len(closes), max(cfg.ema_slow + 2, cfg.rsi_period + 1), -1):
        snap = evaluate(closes[:n], cfg)[1]
        hit = snap.crossed_up if direction == "up" else snap.crossed_down
        if not hit:
            continue
        prev = evaluate(closes[: n - 1], cfg)[1]
        prev_hit = prev.crossed_up if direction == "up" else prev.crossed_down
        if not prev_hit:
            return n
    raise AssertionError(f"no {direction} cross found in the series")


# ── EMA ───────────────────────────────────────────────────────────────────


def test_ema_seeded_with_sma() -> None:
    # Period 3: SMA seed = (1+2+3)/3 = 2.0, k = 2/4 = 0.5
    # next = 4*0.5 + 2*0.5 = 3.0 ; next = 5*0.5 + 3*0.5 = 4.0
    result = ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert result == [2.0, 3.0, 4.0], result
    print("ok  ema seeded with SMA")


def test_ema_insufficient_data() -> None:
    assert ema([1.0, 2.0], 5) == []
    print("ok  ema returns [] on short input")


def test_ema_flat_series_is_constant() -> None:
    assert all(approx(v, 10.0) for v in ema([10.0] * 50, 9))
    print("ok  ema of flat series stays flat")


def test_ema_lags_on_constant_slope() -> None:
    """On a rising line the EMA must lag below price and stay monotonic."""
    closes = [float(i) for i in range(100)]
    series = ema(closes, 9)

    # Monotonic input -> monotonic EMA.
    assert all(series[i] < series[i + 1] for i in range(len(series) - 1)), series[:5]
    # An EMA is a convex combination of past prices, so it stays in range.
    assert all(closes[0] <= v <= closes[-1] for v in series)
    # The seed is the SMA of the first 9 prices = 0..8 -> midpoint 4.0.
    assert approx(series[0], 4.0), series[0]
    # And it lags the final price.
    assert series[-1] < closes[-1]
    print("ok  ema lags price on a constant slope")


# ── RSI ───────────────────────────────────────────────────────────────────


def test_rsi_length() -> None:
    """RSI emits one value per candle from `period` onward: len(closes) - period."""
    for period in (2, 3, 14):
        closes = [float(i % 17) + 10 for i in range(60)]
        assert len(rsi(closes, period)) == len(closes) - period, period
    print("ok  rsi output length == len(closes) - period")


def test_rsi_all_gains_is_100() -> None:
    closes = [float(i) for i in range(1, 40)]
    result = rsi(closes, 14)
    assert len(result) == len(closes) - 14, len(result)
    assert all(approx(v, 100.0) for v in result), result[:5]
    print("ok  rsi of monotonic rise is 100")


def test_rsi_all_losses_is_0() -> None:
    closes = [float(i) for i in range(40, 1, -1)]
    assert all(approx(v, 0.0) for v in rsi(closes, 14))
    print("ok  rsi of monotonic fall is 0")


def test_rsi_hand_computed_tiny_series() -> None:
    """Worked by hand: first window is gains (1,0,1) / losses (0,1,0)."""
    closes = [10, 11, 10, 11, 12, 11, 12, 13, 12, 13, 14, 13, 14, 15, 14, 15, 16]
    result = rsi(closes, 3)
    # avg_gain = 2/3, avg_loss = 1/3 -> RS = 2 -> RSI = 100 - 100/3
    assert approx(result[0], 66.6667, tol=1e-4), result[0]
    # avg_gain = (2/3*2 + 1)/3 = 0.7778, avg_loss = (1/3*2 + 0)/3 = 0.2222
    # -> RS = 3.5 -> RSI = 100 - 100/4.5
    assert approx(result[1], 77.7778, tol=1e-4), result[1]
    print("ok  rsi matches hand calculation")


def test_rsi_matches_reference() -> None:
    """SUT must agree with the independent implementation, point for point."""
    import random

    rng = random.Random(1234)
    price = 100.0
    closes = []
    for _ in range(200):
        price *= 1.0 + rng.uniform(-0.04, 0.04)
        closes.append(price)

    for period in (2, 7, 14, 21):
        mine, ref = rsi(closes, period), wilder_rsi_ref(closes, period)
        assert len(mine) == len(ref), (len(mine), len(ref))
        for a, b in zip(mine, ref):
            assert approx(a, b, tol=1e-9), (period, a, b)
        assert all(0.0 <= v <= 100.0 for v in mine), period
    print("ok  rsi agrees with independent reference (4 periods, 200 candles)")


# ── signals ───────────────────────────────────────────────────────────────


def _trend_then_reversal() -> list[float]:
    """Steady, a deep decline, then a recovery. Produces a cross-up."""
    closes = [100.0] * 40
    closes += [100 - i * 1.5 for i in range(1, 31)]
    bottom = closes[-1]
    closes += [bottom + i * 4.0 for i in range(1, 26)]
    return closes


def test_cross_up_produces_buy() -> None:
    cfg = make_cfg()
    closes = _trend_then_reversal()
    n = find_cross(closes, cfg, "up")
    signal, snap = evaluate(closes[:n], cfg)
    assert snap.crossed_up is True, snap
    assert snap.ema_fast > snap.ema_slow, snap
    assert signal is Signal.BUY, signal
    print(f"ok  cross-up -> BUY (rsi={snap.rsi:.1f})")


def test_cross_down_produces_sell() -> None:
    cfg = make_cfg()
    # Mirror image: rally, then a decline.
    closes = [100.0] * 40
    closes += [100 + i * 1.5 for i in range(1, 31)]
    top = closes[-1]
    closes += [top - i * 4.0 for i in range(1, 26)]

    n = find_cross(closes, cfg, "down")
    signal, snap = evaluate(closes[:n], cfg)
    assert snap.crossed_down is True, snap
    assert signal is Signal.SELL, signal
    print("ok  cross-down -> SELL")


def test_steady_trend_has_no_cross() -> None:
    cfg = make_cfg()
    signal, snap = evaluate([100.0 + i for i in range(60)], cfg)
    assert snap.crossed_up is False and snap.crossed_down is False, snap
    assert signal is Signal.HOLD, signal
    print("ok  steady trend -> HOLD (no cross)")


def test_overbought_cross_is_filtered_to_hold() -> None:
    """A cross-up inside an extreme rally must not buy."""
    cfg = make_cfg(rsi_max_entry=70.0)
    # Sweep a grid of market shapes until one produces the situation under
    # test (a cross-up while RSI is overbought), then assert the filter fires.
    for drop in (0.02, 0.03, 0.05, 0.08):
        for rally in (0.05, 0.08, 0.10, 0.15, 0.20):
            closes = [100.0]
            for _ in range(30):
                closes.append(closes[-1] * (1 - drop))
            for _ in range(45):
                closes.append(closes[-1] * (1 + rally))

            for n in range(len(closes), max(cfg.ema_slow + 2, cfg.rsi_period + 1), -1):
                signal, snap = evaluate(closes[:n], cfg)
                if snap.crossed_up and snap.rsi > cfg.rsi_max_entry:
                    assert signal is Signal.HOLD, (signal, snap)
                    print(
                        f"ok  overbought cross -> HOLD "
                        f"(rsi={snap.rsi:.1f}, drop={drop}, rally={rally})"
                    )
                    return
    raise AssertionError("no overbought cross-up found across the fixture grid")


def test_oversold_cross_is_filtered_to_hold() -> None:
    """A cross-up in free-fall must not buy: the falling-knife guard.

    NOTE: this condition is only reachable with FAST EMA settings. With the
    defaults (9/21) a cross-up structurally requires enough sustained
    upside that RSI never gets below ~37, so the guard is dormant there.
    See test_oversold_guard_is_dormant_at_default_emas.
    """
    cfg = make_cfg(ema_fast=5, ema_slow=8, rsi_min_entry=20.0, rsi_max_entry=70.0)
    for crash in (0.02, 0.05, 0.10, 0.20):
        for nc in (20, 40):
            for bounce in (0.002, 0.005, 0.01, 0.02):
                closes = [100.0]
                for _ in range(nc):
                    closes.append(closes[-1] * (1 - crash))
                for _ in range(25):
                    closes.append(closes[-1] * (1 + bounce))

                for n in range(max(cfg.ema_slow + 2, cfg.rsi_period + 1), len(closes) + 1):
                    signal, snap = evaluate(closes[:n], cfg)
                    if snap.crossed_up and snap.rsi < cfg.rsi_min_entry:
                        assert signal is Signal.HOLD, (signal, snap)
                        print(
                            f"ok  oversold cross -> HOLD "
                            f"(rsi={snap.rsi:.1f}, EMA {cfg.ema_fast}/{cfg.ema_slow})"
                        )
                        return
    raise AssertionError("no oversold cross-up found across the fixture grid")


def test_oversold_guard_is_dormant_at_default_emas() -> None:
    """Documents a real property of the default parameters.

    With EMA 9/21 the cross-up needs enough sustained upside that RSI never
    falls below ~37, so RSI_MIN_ENTRY will not block anything. Raise this
    expectation if you change the default EMAs.
    """
    cfg = make_cfg()  # 9/21, rsi_min_entry=20
    lowest = 100.0
    for crash in (0.01, 0.02, 0.05, 0.10, 0.20):
        for nc in (20, 40):
            for bounce in (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.10):
                for nb in (10, 25):
                    closes = [100.0]
                    for _ in range(nc):
                        closes.append(closes[-1] * (1 - crash))
                    for _ in range(nb):
                        closes.append(closes[-1] * (1 + bounce))
                    for n in range(max(cfg.ema_slow + 2, cfg.rsi_period + 1), len(closes) + 1):
                        snap = evaluate(closes[:n], cfg)[1]
                        if snap.crossed_up:
                            lowest = min(lowest, snap.rsi)
    assert lowest > 35.0, lowest
    print(f"ok  default EMA 9/21: lowest RSI at any cross-up = {lowest:.1f} (guard dormant)")


def test_no_cross_is_never_a_signal() -> None:
    cfg = make_cfg()
    import random

    rng = random.Random(99)
    price = 100.0
    closes = []
    for _ in range(150):
        price *= 1.0 + rng.uniform(-0.03, 0.03)
        closes.append(price)

    # At every candle, a non-cross must map to HOLD.
    for n in range(max(cfg.ema_slow + 2, cfg.rsi_period + 1), len(closes) + 1):
        signal, snap = evaluate(closes[:n], cfg)
        if not snap.crossed_up and not snap.crossed_down:
            assert signal is Signal.HOLD, (n, signal, snap)
    print("ok  no cross is never a signal")


def test_insufficient_history_raises() -> None:
    cfg = make_cfg()
    for bad in ([], [1.0] * 5, [1.0] * 20):
        try:
            evaluate(bad, cfg)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {len(bad)} candles")
    print("ok  insufficient history raises ValueError")


def test_indicator_series_lengths_match_warmup() -> None:
    """Each overlay is shorter than the candles by exactly its warm-up."""
    closes = [100.0 + i for i in range(60)]
    out = indicator_series(closes, make_cfg())

    # ema(p) emits len-p+1 values; rsi(p) emits len-p. The reported start is
    # the index into `closes` that the first emitted value belongs to.
    assert out["emaFast"]["start"] == 8, out["emaFast"]
    assert len(out["emaFast"]["values"]) == 52, out["emaFast"]
    assert out["emaSlow"]["start"] == 20, out["emaSlow"]
    assert len(out["emaSlow"]["values"]) == 40, out["emaSlow"]
    assert out["rsi"]["start"] == 14, out["rsi"]
    assert len(out["rsi"]["values"]) == 46, out["rsi"]
    print("ok  indicator series lengths match their warm-up")


def test_indicator_series_ends_on_the_last_candle() -> None:
    """start + len == candle count, so the chart cannot drift out of sync."""
    closes = [100.0 + (i % 7) * 0.5 for i in range(80)]
    out = indicator_series(closes, make_cfg())
    n = len(closes)
    for name, pack in out.items():
        assert pack["start"] + len(pack["values"]) == n, (name, pack)
    print("ok  indicator series all end on the last candle")


def test_indicator_series_matches_standalone_functions() -> None:
    """The overlay must agree with what the trading loop actually computes.

    A divergence here would mean the chart shows one thing and the bot
    decides on another.
    """
    closes = [100.0 + (i % 11) * 0.3 - (i % 3) for i in range(90)]
    cfg = make_cfg()
    out = indicator_series(closes, cfg)

    assert approx(out["emaFast"]["values"][-1], ema(closes, cfg.ema_fast)[-1])
    assert approx(out["emaSlow"]["values"][-1], ema(closes, cfg.ema_slow)[-1])
    assert approx(out["rsi"]["values"][-1], rsi(closes, cfg.rsi_period)[-1])
    print("ok  indicator series agrees with the standalone indicators")


def test_indicator_series_empty_without_history() -> None:
    """Too little data yields empty series, never a crash."""
    out = indicator_series([100.0, 101.0], make_cfg())
    for name in ("emaFast", "emaSlow", "rsi"):
        assert out[name]["values"] == [], (name, out[name])
    print("ok  indicator series is empty without enough history")


def main() -> int:
    tests = [
        test_ema_seeded_with_sma,
        test_ema_insufficient_data,
        test_ema_flat_series_is_constant,
        test_ema_lags_on_constant_slope,
        test_rsi_length,
        test_rsi_all_gains_is_100,
        test_rsi_all_losses_is_0,
        test_rsi_hand_computed_tiny_series,
        test_rsi_matches_reference,
        test_cross_up_produces_buy,
        test_cross_down_produces_sell,
        test_steady_trend_has_no_cross,
        test_overbought_cross_is_filtered_to_hold,
        test_oversold_cross_is_filtered_to_hold,
        test_oversold_guard_is_dormant_at_default_emas,
        test_no_cross_is_never_a_signal,
        test_insufficient_history_raises,
        test_indicator_series_lengths_match_warmup,
        test_indicator_series_ends_on_the_last_candle,
        test_indicator_series_matches_standalone_functions,
        test_indicator_series_empty_without_history,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")

    print()
    if failed:
        print(f"{failed}/{len(tests)} tests FAILED")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
