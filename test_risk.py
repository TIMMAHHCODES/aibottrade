"""Self-test for risk management and state persistence. No network access.

These are the guardrails around real money, so they get their own suite.

Run:  python test_risk.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.config import Config  # noqa: E402
from bot.risk import ExitReason, RiskManager  # noqa: E402
from bot.state import StateStore  # noqa: E402


def make_cfg(tmp: Path, **overrides) -> Config:
    """A valid live-mode config; tests override only what they exercise."""
    base = dict(
        dry_run=False,
        private_key_base58="x",
        base_mint="BASE_MINT_BASE_MINT_BASE_MINT_BASE_MINT1111111111",
        base_symbol="SOL",
        quote_mint="QUOTE_MINT_QUOTE_MINT_QUOTE_MINT_QUOTE_MINT11111",
        quote_symbol="USDC",
        timeframe_minutes=15,
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        rsi_max_entry=65.0,
        rsi_min_entry=35.0,
        trade_size_quote=5.0,
        max_quote_per_day=20.0,
        max_open_positions=2,
        slippage_bps=100,
        take_profit_pct=0.04,
        stop_loss_pct=0.03,
        quote_reserve=0.05,
        cooldown_seconds=120,
        ui_host="127.0.0.1",
        ui_port=8787,
        ui_enabled=False,
        rpc_url="http://localhost",
        jupiter_base_url="http://localhost/swap/v1",
        geckoterminal_base_url="http://localhost/api/v2",
        kill_switch_file=tmp / "kill.txt",
        base_decimals=9,
        quote_decimals=6,
        working_dir=tmp,
    )
    base.update(overrides)
    return Config(**base)


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="bot-test-"))


# ── kill switch ───────────────────────────────────────────────────────────


def test_kill_switch_blocks_entry() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp)
    risk = RiskManager(cfg)
    store = StateStore(tmp)

    assert risk.check_entry(store, 100.0, 1e9) is None, "should allow entry when clear"

    cfg.kill_switch_file.write_text("stop", encoding="utf-8")
    reason = risk.check_entry(store, 100.0, 1e9)
    assert reason is not None and "kill switch" in reason, reason
    print("ok  kill switch blocks new entries")


# ── position limits ──────────────────────────────────────────────────────


def test_max_open_positions() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp, max_open_positions=2)
    risk = RiskManager(cfg)
    store = StateStore(tmp)

    store.open_position(1_000_000_000, 5_000_000, 5.0, None)
    assert risk.check_entry(store, 100.0, 1e9) is None

    store.open_position(1_000_000_000, 5_000_000, 5.0, None)
    reason = risk.check_entry(store, 100.0, 1e9)
    assert reason is not None and "MAX_OPEN_POSITIONS" in reason, reason
    print("ok  refuses to exceed MAX_OPEN_POSITIONS")


def test_daily_budget() -> None:
    tmp = tmpdir()
    # Raise the position cap so this test isolates the budget constraint.
    cfg = make_cfg(tmp, trade_size_quote=5.0, max_quote_per_day=20.0,
                   max_open_positions=100)
    risk = RiskManager(cfg)
    store = StateStore(tmp)

    # Each fill is 5 USDC (6 decimals = 5_000_000 units).
    for i in range(3):
        store.open_position(1_000_000_000, 5_000_000, 5.0, None)
        assert risk.check_entry(store, 1000.0, 1e9) is None, f"fill {i+1} should be allowed"

    store.open_position(1_000_000_000, 5_000_000, 5.0, None)  # 4th -> 20 spent
    reason = risk.check_entry(store, 1000.0, 1e9)
    assert reason is not None and "daily budget" in reason, reason
    assert store.state.daily_quote_spent == 20_000_000, store.state.daily_quote_spent
    print("ok  refuses to exceed the daily budget")


def test_daily_budget_rolls_over_on_new_utc_day() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp, trade_size_quote=5.0, max_quote_per_day=10.0,
                   max_open_positions=100)
    risk = RiskManager(cfg)
    store = StateStore(tmp)

    store.open_position(1_000_000_000, 5_000_000, 5.0, None)
    store.open_position(1_000_000_000, 5_000_000, 5.0, None)
    assert risk.check_entry(store, 1000.0, 1e9) is not None, "should be capped"

    # Simulate the clock rolling over.
    store.state.daily_date = "2000-01-01"
    assert risk.check_entry(store, 1000.0, 1e9) is None, "should reset on a new day"
    assert store.state.daily_quote_spent == 0
    print("ok  daily budget resets when the UTC day changes")


# ── balance ──────────────────────────────────────────────────────────────


def test_balance_checks() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp, trade_size_quote=5.0, quote_reserve=0.05)
    risk = RiskManager(cfg)
    store = StateStore(tmp)

    reason = risk.check_entry(store, 5.04, 1e9)  # needs 5.05
    assert reason is not None and "insufficient" in reason, reason

    assert risk.check_entry(store, 5.05, 1e9) is None
    assert risk.check_entry(store, None, 1e9) is None, "unknown balance must not block"
    print("ok  balance and reserve checks")


def test_cooldown() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp, cooldown_seconds=120)
    risk = RiskManager(cfg)
    store = StateStore(tmp)

    import time as _time

    assert risk.check_entry(store, 100.0, 0.0) is not None
    assert risk.check_entry(store, 100.0, 119.0) is not None
    assert risk.check_entry(store, 100.0, 121.0) is None
    assert _time.time() > 0  # sanity
    print("ok  cooldown blocks entries between fills")


# ── sizing ───────────────────────────────────────────────────────────────


def test_clamp_buy_amount_never_sizes_up() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp, trade_size_quote=5.0, quote_reserve=0.05, quote_decimals=6)
    risk = RiskManager(cfg)

    # A huge wallet does not make the bot bet more.
    assert risk.clamp_buy_amount(1_000_000.0) == 5_000_000
    assert risk.clamp_buy_amount(None) == 5_000_000
    # A thin wallet clamps down, and never below zero.
    assert risk.clamp_buy_amount(3.0) == 2_950_000
    assert risk.clamp_buy_amount(0.01) == 0
    print("ok  position sizing is fixed, never scaled by balance")


# ── exits ────────────────────────────────────────────────────────────────


def test_take_profit_and_stop_loss() -> None:
    tmp = tmpdir()
    cfg = make_cfg(tmp, take_profit_pct=0.04, stop_loss_pct=0.03)
    risk = RiskManager(cfg)

    def pos(entry: float):
        return type("P", (), {"entry_price": entry})()

    p = pos(100.0)
    assert risk.check_exit(p, 103.5, False) is None, "small gain should hold"
    assert risk.check_exit(p, 97.5, False) is None, "small loss should hold"
    assert risk.check_exit(p, 104.0, False) is ExitReason.TAKE_PROFIT
    assert risk.check_exit(p, 97.0, False) is ExitReason.STOP_LOSS
    assert risk.check_exit(p, 100.5, True) is ExitReason.SIGNAL
    print("ok  take-profit / stop-loss / signal exits")


def test_stop_loss_reported_when_both_levels_breached() -> None:
    """A gap through both levels must be logged as a stop-loss."""
    tmp = tmpdir()
    cfg = make_cfg(tmp, take_profit_pct=0.04, stop_loss_pct=0.03)
    risk = RiskManager(cfg)
    p = type("P", (), {"entry_price": 100.0})()
    # Down 20% AND the strategy says sell: loss wins the ordering.
    assert risk.check_exit(p, 80.0, True) is ExitReason.STOP_LOSS
    print("ok  stop-loss takes precedence over the sell signal")


def test_zero_prices_do_not_crash_exits() -> None:
    tmp = tmpdir()
    risk = RiskManager(make_cfg(tmp))
    p = type("P", (), {"entry_price": 0.0})()
    assert risk.check_exit(p, 0.0, False) is None
    assert risk.check_exit(p, 100.0, False) is None
    print("ok  zero/garbage prices are ignored rather than acted on")


# ── state persistence ────────────────────────────────────────────────────


def test_state_round_trip() -> None:
    tmp = tmpdir()
    store = StateStore(tmp)
    p1 = store.open_position(2_000_000_000, 5_000_000, 2.5, "sig1")
    p2 = store.open_position(1_000_000_000, 3_000_000, 3.0, None)

    reloaded = StateStore(tmp)
    assert len(reloaded.state.positions) == 2
    assert reloaded.state.positions[0].id == p1.id
    assert reloaded.state.positions[1].id == p2.id
    assert reloaded.state.positions[0].base_amount == 2_000_000_000
    assert reloaded.state.daily_quote_spent == 8_000_000
    print("ok  state survives a save/load round trip")


def test_close_position() -> None:
    tmp = tmpdir()
    store = StateStore(tmp)
    p = store.open_position(1_000_000_000, 5_000_000, 5.0, None)

    assert store.close_position(p.id) is not None
    assert store.state.positions == []
    assert store.close_position("nope") is None, "unknown id should be a no-op"
    print("ok  closing a position removes it exactly once")


def test_amounts_are_integers_not_floats() -> None:
    """Float accumulation would silently drift; assert we store ints."""
    tmp = tmpdir()
    store = StateStore(tmp)
    # 5 USDC = 5_000_000 units. Repeating this a few times must not drift.
    for _ in range(10):
        store.open_position(1, 5_000_000, 5.0, None)

    raw = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
    for pos in raw["positions"]:
        assert isinstance(pos["quote_spent"], int), pos
        assert isinstance(pos["base_amount"], int), pos
    assert raw["daily_quote_spent"] == 50_000_000
    print("ok  stored amounts stay exact integers over 10 fills")


def test_unrealized_pnl_math() -> None:
    tmp = tmpdir()
    store = StateStore(tmp)
    p = store.open_position(2_000_000_000, 10_000_000, 5.0, None)  # 2 SOL for 10 USDC
    # base_amount = 2e9 units = 2 SOL (9 decimals); quote_spent = 10 USDC.
    # Decimals must be passed explicitly -- silently mixing the unit scales
    # is exactly the bug this signature prevents.
    assert abs(p.unrealized_pnl_quote_units(6.0, 9, 6) - 2.0) < 1e-9, "up 2 USDC"
    assert abs(p.unrealized_pnl_quote_units(5.0, 9, 6) - 0.0) < 1e-9, "flat"
    assert abs(p.unrealized_pnl_quote_units(4.0, 9, 6) - (-2.0)) < 1e-9, "down 2 USDC"
    print("ok  unrealized PnL math")


def test_corrupt_state_is_refused_not_overwritten() -> None:
    tmp = tmpdir()
    (tmp / "state.json").write_text("{not json", encoding="utf-8")
    try:
        StateStore(tmp)
    except RuntimeError as exc:
        assert "refusing to overwrite" in str(exc), exc
        # The corrupt file must be left intact for inspection.
        assert (tmp / "state.json").read_text(encoding="utf-8") == "{not json"
        print("ok  corrupt state.json is refused, not silently clobbered")
        return
    raise AssertionError("expected RuntimeError on corrupt state")


def test_trade_log_is_append_only() -> None:
    tmp = tmpdir()
    store = StateStore(tmp)
    store.log_trade({"side": "BUY", "quote_spent": 5.0})
    store.log_trade({"side": "SELL", "pnl_quote": -0.1})

    lines = (tmp / "trades.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, lines
    assert json.loads(lines[0])["side"] == "BUY"
    assert json.loads(lines[1])["side"] == "SELL"
    assert all("logged_at" in json.loads(line) for line in lines)
    print("ok  trade log appends one record per fill")


def main() -> int:
    tests = [
        test_kill_switch_blocks_entry,
        test_max_open_positions,
        test_daily_budget,
        test_daily_budget_rolls_over_on_new_utc_day,
        test_balance_checks,
        test_cooldown,
        test_clamp_buy_amount_never_sizes_up,
        test_take_profit_and_stop_loss,
        test_stop_loss_reported_when_both_levels_breached,
        test_zero_prices_do_not_crash_exits,
        test_state_round_trip,
        test_close_position,
        test_amounts_are_integers_not_floats,
        test_unrealized_pnl_math,
        test_corrupt_state_is_refused_not_overwritten,
        test_trade_log_is_append_only,
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
