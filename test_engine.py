"""End-to-end test of the trading loop, with the network faked out.

The strategy tests prove the indicator math. This suite proves the parts
that touch money: that a signal actually opens a position, that take-profit
and stop-loss actually close it, that PnL is computed in the right units,
and that the daily budget really stops further entries.

Run:  python test_engine.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.config import Config  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.jupiter import Fill, Quote  # noqa: E402
from bot.strategy import evaluate  # noqa: E402


def make_cfg(tmp: Path, **overrides) -> Config:
    base = dict(
        dry_run=True,
        private_key_base58="",
        base_mint="So11111111111111111111111111111111111111112",
        base_symbol="SOL",
        quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        quote_symbol="USDC",
        timeframe_minutes=15,
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        rsi_max_entry=70.0,
        rsi_min_entry=20.0,
        trade_size_quote=5.0,
        max_quote_per_day=20.0,
        max_open_positions=2,
        slippage_bps=100,
        take_profit_pct=0.04,
        stop_loss_pct=0.03,
        quote_reserve=0.05,
        cooldown_seconds=0,
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


class FakeMarket:
    """Serves a fixed candle series and a settable live price."""

    def __init__(self, cfg: Config, closes: list[float]):
        self.cfg = cfg
        self.closes = closes
        self.price = closes[-1]
        self.closes_calls = 0
        self.price_calls = 0

    def fetch_closes(self, limit: int = 250) -> list[float]:
        self.closes_calls += 1
        return list(self.closes)

    def fetch_price(self) -> float:
        self.price_calls += 1
        return self.price

    def resolve_pool(self) -> str:
        return "fakepool"

    def close(self) -> None:
        pass


class FakeJupiter:
    """Fills at whatever price the engine is currently reading."""

    def __init__(self, cfg: Config, price_provider):
        self.cfg = cfg
        self.price_provider = price_provider
        self.quotes: list[tuple[int, int]] = []
        self.executed: list[tuple[int, int]] = []

    def get_quote(self, input_mint: str, output_mint: str, amount: int) -> Quote:
        self.quotes.append((input_mint, output_mint, amount))
        price = self.price_provider()

        if input_mint == self.cfg.quote_mint:
            # Buying base with quote.
            in_ui = amount / (10 ** self.cfg.quote_decimals)
            out_amount = int((in_ui / price) * (10 ** self.cfg.base_decimals))
        else:
            # Selling base for quote.
            in_ui = amount / (10 ** self.cfg.base_decimals)
            out_amount = int((in_ui * price) * (10 ** self.cfg.quote_decimals))

        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=out_amount,
            other_amount_threshold=int(out_amount * 0.99),
            slippage_bps=self.cfg.slippage_bps,
            price_impact_pct=0.0,
            raw={"_fake": True},
        )

    def execute(self, quote: Quote, dry_run: bool) -> Fill:
        self.executed.append((quote.in_amount, quote.out_amount))
        return Fill(
            signature=None if dry_run else "FAKE_SIGNATURE",
            input_mint=quote.input_mint,
            output_mint=quote.output_mint,
            in_amount=quote.in_amount,
            out_amount=quote.out_amount,
            dry_run=dry_run,
        )

    def close(self) -> None:
        pass


# A steady uptrend has no fresh crossover, so evaluate() returns HOLD. Tests
# swap this in after the entry so later cycles do not re-trigger the BUY
# cross -- in reality a cross happens once, not on every candle.
HOLD_SERIES = [100.0 + i for i in range(60)]


class FakeWallet:
    """Stands in for the real wallet so live-mode tests never hit RPC."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pubkey = "FAKE_PUBKEY"

    def get_token_balance(self, mint: str) -> int:
        return 10 ** self.cfg.base_decimals  # "plenty"

    def get_sol_lamports(self) -> int:
        return 1_000_000_000

    def sign_and_serialize(self, tx_b64: str) -> str:
        return tx_b64

    def send_raw(self, tx_b64: str) -> str:
        return "FAKE_SIGNATURE"

    def close(self) -> None:
        pass


def build_engine(tmp: Path, closes: list[float], **cfg_overrides) -> tuple[Engine, FakeMarket, FakeJupiter]:
    cfg = make_cfg(tmp, **cfg_overrides)
    engine = Engine(cfg)
    market = FakeMarket(cfg, closes)
    jupiter = FakeJupiter(cfg, lambda: market.price)
    engine.market = market
    engine.jupiter = jupiter
    if not cfg.dry_run:
        engine.wallet = FakeWallet(cfg)
    return engine, market, jupiter


def series_with_cross_up(cfg: Config) -> list[float]:
    """Find a candle history whose final candle is a fresh cross-up."""
    closes = [100.0] * 40
    closes += [100 - i * 1.5 for i in range(1, 31)]
    bottom = closes[-1]
    closes += [bottom + i * 4.0 for i in range(1, 40)]

    for n in range(len(closes), cfg.ema_slow + 2, -1):
        snap = evaluate(closes[:n], cfg)[1]
        if snap.crossed_up:
            prev = evaluate(closes[: n - 1], cfg)[1]
            if not prev.crossed_up:
                return closes[:n]
    raise AssertionError("fixture did not produce a cross-up")


def read_trades(tmp: Path) -> list[dict]:
    path = tmp / "trades.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]


# ── entry ────────────────────────────────────────────────────────────────


def test_buy_signal_opens_a_position() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        status = engine.run_once()
        assert status == "bought", status

        positions = engine.store.state.positions
        assert len(positions) == 1, positions
        p = positions[0]

        # 5 USDC spent, executed at whatever the fixture's last price is.
        price = market.price
        expected_base_units = int((5.0 / price) * (10 ** engine.cfg.base_decimals))
        assert abs(p.quote_spent - 5_000_000) < 2, p.quote_spent
        assert abs(p.base_amount - expected_base_units) <= 1, (p.base_amount, expected_base_units)
        assert abs(p.entry_price - price) < price * 0.01, (p.entry_price, price)
        assert p.signature is None, "dry run must not carry a signature"

        trades = read_trades(tmp)
        assert len(trades) == 1, trades
        assert trades[0]["side"] == "BUY"
        assert trades[0]["mode"] == "dry_run"
        assert trades[0]["signature"] is None
        assert engine.store.state.daily_quote_spent == 5_000_000
        print("ok  BUY signal opens a position and logs it")
    finally:
        engine.close()


# ── exits ────────────────────────────────────────────────────────────────


def test_take_profit_closes_with_a_gain() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1
        entry = engine.store.state.positions[0].entry_price
        # A cross fires once. Switch to a flat series so later cycles HOLD
        # instead of re-entering on the same (already consumed) signal.
        market.closes = HOLD_SERIES

        # +5% from entry, above the 4% take-profit threshold.
        market.price = entry * 1.05
        engine.run_once()

        assert engine.store.state.positions == [], "position should be closed"
        trades = read_trades(tmp)
        assert [t["side"] for t in trades] == ["BUY", "SELL"], trades

        sell = trades[1]
        assert sell["exit_reason"] == "take_profit", sell
        assert sell["mode"] == "dry_run"
        # 5.00 USDC in, +5% -> 5.25 USDC back.
        assert abs(sell["quote_received"] - 5.25) < 0.01, sell
        assert abs(sell["pnl_quote"] - 0.25) < 0.01, sell
        assert sell["pnl_pct"] > 0, sell
        print(f"ok  take-profit closes at +{sell['pnl_pct']:.2f}%")
    finally:
        engine.close()


def test_stop_loss_closes_with_a_loss() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1
        entry = engine.store.state.positions[0].entry_price
        # A cross fires once. Switch to a flat series so later cycles HOLD
        # instead of re-entering on the same (already consumed) signal.
        market.closes = HOLD_SERIES

        market.price = entry * 0.95  # -5%, past the 3% stop-loss
        engine.run_once()

        assert engine.store.state.positions == []
        sell = read_trades(tmp)[1]
        assert sell["exit_reason"] == "stop_loss", sell
        assert abs(sell["pnl_quote"] - (-0.25)) < 0.01, sell
        assert sell["pnl_pct"] < 0, sell
        print(f"ok  stop-loss closes at {sell['pnl_pct']:.2f}%")
    finally:
        engine.close()


def test_small_move_does_not_exit() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1
        entry = engine.store.state.positions[0].entry_price
        # A cross fires once. Switch to a flat series so later cycles HOLD
        # instead of re-entering on the same (already consumed) signal.
        market.closes = HOLD_SERIES

        # +2%, inside the 4%/3% bands.
        market.price = entry * 1.02
        engine.run_once()
        assert len(engine.store.state.positions) == 1, "should still be held"
        assert [t["side"] for t in read_trades(tmp)] == ["BUY"]
        print("ok  a move inside the bands does not exit")
    finally:
        engine.close()


def test_exit_happens_even_with_kill_switch() -> None:
    """The kill switch blocks entries but must never trap you in a position."""
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1
        entry = engine.store.state.positions[0].entry_price
        # A cross fires once. Switch to a flat series so later cycles HOLD
        # instead of re-entering on the same (already consumed) signal.
        market.closes = HOLD_SERIES

        engine.cfg.kill_switch_file.write_text("stop", encoding="utf-8")
        market.price = entry * 0.95
        engine.run_once()

        assert engine.store.state.positions == [], "kill switch must not block exits"
        assert read_trades(tmp)[1]["exit_reason"] == "stop_loss"
        print("ok  kill switch still allows exits")
    finally:
        engine.close()


def test_kill_switch_blocks_new_entries() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.cfg.kill_switch_file.write_text("stop", encoding="utf-8")
        status = engine.run_once()

        assert status.startswith("blocked"), status
        assert "kill switch" in status, status
        assert engine.store.state.positions == []
        assert jupiter.executed == [], "no swap must be executed"
        print("ok  kill switch blocks entries before any swap")
    finally:
        engine.close()


# ── budget ───────────────────────────────────────────────────────────────


def test_daily_budget_stops_further_buys() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    # 5 USDC per trade, 12 USDC per day -> exactly 2 trades allowed.
    engine, market, jupiter = build_engine(
        tmp, closes, max_quote_per_day=12.0, max_open_positions=10,
        take_profit_pct=0.99, stop_loss_pct=0.99,  # suppress exits
    )
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1

        # Force another BUY cross on the following cycle.
        engine.run_once()
        assert len(engine.store.state.positions) == 2

        status = engine.run_once()
        assert status.startswith("blocked"), status
        assert "daily budget" in status, status
        assert len(engine.store.state.positions) == 2, "must not open a third"
        assert engine.store.state.daily_quote_spent == 10_000_000
        print("ok  daily budget stops further buys")
    finally:
        engine.close()


def test_max_open_positions_blocks_entries() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(
        tmp, closes, max_open_positions=1, take_profit_pct=0.99, stop_loss_pct=0.99,
    )
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1

        status = engine.run_once()
        assert status.startswith("blocked"), status
        assert "MAX_OPEN_POSITIONS" in status, status
        assert len(engine.store.state.positions) == 1
        print("ok  MAX_OPEN_POSITIONS blocks a second entry")
    finally:
        engine.close()


# ── state integrity ──────────────────────────────────────────────────────


def test_positions_survive_a_restart() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.run_once()
        opened_id = engine.store.state.positions[0].id
        market.closes = HOLD_SERIES
    finally:
        engine.close()

    # Simulate a process restart: a brand new Engine reads the same dir.
    engine2, market2, _ = build_engine(tmp, closes)
    market2.closes = HOLD_SERIES
    try:
        assert len(engine2.store.state.positions) == 1
        assert engine2.store.state.positions[0].id == opened_id

        entry = engine2.store.state.positions[0].entry_price
        market2.price = entry * 1.05
        engine2.run_once()
        assert engine2.store.state.positions == [], "restart then take-profit"
        assert [t["side"] for t in read_trades(tmp)] == ["BUY", "SELL"]
        print("ok  open position survives a restart and still exits")
    finally:
        engine2.close()


def test_dry_run_never_requests_a_signature() -> None:
    """The whole point of dry run: nothing gets signed or broadcast."""
    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    engine, market, jupiter = build_engine(tmp, closes)
    try:
        engine.run_once()
        # Force a full round trip so both the BUY and SELL paths are covered.
        entry = engine.store.state.positions[0].entry_price
        market.closes = HOLD_SERIES
        market.price = entry * 1.05
        engine.run_once()

        trades = read_trades(tmp)
        assert [t["side"] for t in trades] == ["BUY", "SELL"], trades
        assert all(t["signature"] is None for t in trades), trades
        assert all(t["mode"] == "dry_run" for t in trades), trades
        assert all("FAKE" not in json.dumps(t) for t in trades)
        print("ok  dry run never produces a signature")
    finally:
        engine.close()


def test_live_mode_records_a_signature() -> None:
    """Live mode must carry a signature from the swap through to the log."""
    import base58
    from solders.keypair import Keypair

    tmp = Path(tempfile.mkdtemp(prefix="bot-eng-"))
    probe_cfg = make_cfg(tmp)
    closes = series_with_cross_up(probe_cfg)

    # A throwaway key. Jupiter is faked, so nothing real is ever signed.
    key_b58 = base58.b58encode(bytes(Keypair())).decode()

    engine, market, jupiter = build_engine(
        tmp, closes, dry_run=False, private_key_base58=key_b58
    )
    try:
        engine.run_once()
        assert len(engine.store.state.positions) == 1
        entry = engine.store.state.positions[0].entry_price
        # A cross fires once. Switch to a flat series so later cycles HOLD
        # instead of re-entering on the same (already consumed) signal.
        market.closes = HOLD_SERIES

        market.price = entry * 1.05
        engine.run_once()

        trades = read_trades(tmp)
        assert [t["side"] for t in trades] == ["BUY", "SELL"], trades
        assert all(t["mode"] == "live" for t in trades), trades
        assert all(t["signature"] == "FAKE_SIGNATURE" for t in trades), trades
        print("ok  live mode records a signature on every fill")
    finally:
        engine.close()


def main() -> int:
    tests = [
        test_buy_signal_opens_a_position,
        test_take_profit_closes_with_a_gain,
        test_stop_loss_closes_with_a_loss,
        test_small_move_does_not_exit,
        test_exit_happens_even_with_kill_switch,
        test_kill_switch_blocks_new_entries,
        test_daily_budget_stops_further_buys,
        test_max_open_positions_blocks_entries,
        test_positions_survive_a_restart,
        test_dry_run_never_requests_a_signature,
        test_live_mode_records_a_signature,
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
