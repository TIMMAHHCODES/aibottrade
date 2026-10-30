"""Tests for the dashboard's HTTP layer.

The dashboard can stop trades and flip the bot onto real funds, so its
guards are tested rather than trusted. Each test binds a real socket on
loopback, because the parts under test -- header parsing, content-type
checks, origin comparison, routing -- are exactly the parts a mocked
transport would skip.

Offline price lookups are stubbed to fail instantly; without that, every
/api/state call would spend seconds in Jupiter-style retry backoff against
a dead host.
"""

from __future__ import annotations

import json
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import base58
from solders.keypair import Keypair

from bot.config import load_config
from bot.data import DataError, MarketData
from bot.engine import Engine
from bot.web import DashboardServer

# Structurally valid so key derivation succeeds on a live-mode reload, but
# ephemeral and never funded: nothing here signs or broadcasts. It only has
# to be a string that must never be echoed back to a browser.
FAKE_PRIVATE_KEY = base58.b58encode(bytes(Keypair())).decode()

_ORIGINAL_FETCH_PRICE = MarketData.fetch_price

# Deliberately hostile: the host points at a closed port so any accidental
# network call fails fast instead of hanging the suite.
ENV_TEMPLATE = """\
# test fixture -- comments must survive settings edits
DRY_RUN=true
PRIVATE_KEY_BASE58={key}
BASE_MINT=So11111111111111111111111111111111111111112
BASE_SYMBOL=SOL
QUOTE_MINT=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
QUOTE_SYMBOL=USDC
TIMEFRAME_MINUTES=15
EMA_FAST=9
EMA_SLOW=21
RSI_PERIOD=14
RSI_MAX_ENTRY=70
RSI_MIN_ENTRY=20
TRADE_SIZE_QUOTE=5.0
MAX_QUOTE_PER_DAY=20.0
MAX_OPEN_POSITIONS=2
SLIPPAGE_BPS=100
TAKE_PROFIT_PCT=0.04
STOP_LOSS_PCT=0.03
QUOTE_RESERVE=0.05
COOLDOWN_SECONDS=0
UI_ENABLED=true
UI_HOST=127.0.0.1
UI_PORT={port}
KILL_SWITCH_FILE=kill.txt
RPC_URL=http://127.0.0.1:1
JUPITER_BASE_URL=http://127.0.0.1:1/swap/v1
GECKOTERMINAL_BASE_URL=http://127.0.0.1:1/api/v2
"""


def _free_port() -> int:
    """Ask the OS for a port nobody is using."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raise_offline(self):
    raise DataError("price lookup disabled in tests")


class Dashboard:
    """A real dashboard bound to an ephemeral loopback port."""

    def __init__(self, with_key: bool = True):
        # Patched on the class, not the instance: applying a settings change
        # installs a brand new MarketData, and an instance-level stub would
        # vanish exactly when it is needed -- sending the tests to the real
        # API. Tests run one at a time, so this is safe.
        MarketData.fetch_price = _raise_offline

        self.tmp = Path(tempfile.mkdtemp(prefix="bot-web-"))
        self.port = _free_port()
        self.env_path = self.tmp / ".env"
        self.write_env(with_key=with_key)

        # Loaded through the real path so .env parsing and validation are
        # exercised too, not a hand-built Config that could drift.
        cfg = load_config(self.tmp)
        self.engine = Engine(cfg)

        self.server = DashboardServer(self.engine)
        assert self.server.start(), "could not bind the test dashboard"
        self.base = f"http://127.0.0.1:{self.port}"

    def write_env(self, with_key: bool = True) -> None:
        self.env_path.write_text(
            ENV_TEMPLATE.format(
                port=self.port,
                key=FAKE_PRIVATE_KEY if with_key else "",
            ),
            encoding="utf-8",
        )

    def close(self) -> None:
        # Stop the price refresher before restoring the stub, so a lingering
        # refresh cannot reach the real API.
        self.server.stop()
        self.engine.close()
        MarketData.fetch_price = _ORIGINAL_FETCH_PRICE

    def request(
        self,
        path: str,
        method: str = "GET",
        body=None,
        content_type: str = "application/json",
        origin=None,
    ) -> tuple[int, str]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None and content_type:
            req.add_header("Content-Type", content_type)
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def post(self, path: str, body, **kwargs) -> tuple[int, str]:
        return self.request(path, method="POST", body=body, **kwargs)


# ── tests ────────────────────────────────────────────────────────────────


def test_dashboard_serves_the_page():
    d = Dashboard()
    try:
        status, body = d.request("/")
        assert status == 200, status
        assert "Solana Bot" in body, "index.html did not render"
        assert "killBtn" in body, "kill switch control missing from the page"
    finally:
        d.close()
    print("ok  dashboard serves the page")


def test_state_endpoint_reports_a_flat_bot():
    d = Dashboard()
    try:
        status, body = d.request("/api/state")
        assert status == 200, status
        state = json.loads(body)
        assert state["ok"] is True
        assert state["mode"] == "dry_run"
        assert state["positions"] == []
        assert state["risk"]["killSwitch"] is False
        assert state["pnl"]["unrealized"] == 0.0
        assert state["settings"]["EMA_FAST"] == "9"
    finally:
        d.close()
    print("ok  /api/state reports a flat bot")


def test_schema_lists_only_editable_settings():
    d = Dashboard()
    try:
        status, body = d.request("/api/schema")
        assert status == 200, status
        payload = json.loads(body)
        keys = {f["key"] for f in payload["schema"]}
        assert "EMA_FAST" in keys and "TRADE_SIZE_QUOTE" in keys
        # The key must not be part of the schema even as a read-only field:
        # nothing should ever send it anywhere.
        assert "PRIVATE_KEY_BASE58" not in keys, keys
        assert "RPC_URL" not in keys, keys
    finally:
        d.close()
    print("ok  /api/schema lists only editable settings")


def test_post_rejects_non_json_bodies():
    """A form-encoded POST is a 'simple request' that skips CORS preflight.

    Refusing anything but application/json is what keeps a hostile web page
    from driving the bot, since a JSON POST forces a preflight that fails.
    """
    d = Dashboard()
    try:
        for ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
            status, body = d.post(
                "/api/kill", {"engage": True}, content_type=ctype
            )
            assert status == 415, (ctype, status, body)
            assert d.engine.cfg.kill_switch_file.exists() is False, (
                "kill switch was engaged by a non-JSON POST"
            )
    finally:
        d.close()
    print("ok  POST rejects non-JSON bodies")


def test_cross_origin_post_is_refused():
    d = Dashboard()
    try:
        status, body = d.post(
            "/api/kill",
            {"engage": True},
            origin="http://evil.example",
        )
        assert status == 403, (status, body)
        assert d.engine.cfg.kill_switch_file.exists() is False

        # A same-origin Origin header is the legitimate case and must pass.
        status, body = d.post(
            "/api/kill",
            {"engage": True},
            origin=f"http://127.0.0.1:{d.port}",
        )
        assert status == 200, (status, body)
        assert json.loads(body)["killSwitch"] is True
    finally:
        d.close()
    print("ok  cross-origin POST is refused, same-origin is allowed")


def test_go_live_requires_a_typed_confirmation():
    d = Dashboard()
    try:
        # Missing phrase.
        status, body = d.post("/api/mode", {"dryRun": False})
        assert status == 422, (status, body)
        assert "GO LIVE" in body, body

        # Wrong phrase.
        status, body = d.post(
            "/api/mode", {"dryRun": False, "confirm": "yes please"}
        )
        assert status == 422, (status, body)

        # Still dry run.
        assert json.loads(d.request("/api/state")[1])["mode"] == "dry_run"
    finally:
        d.close()
    print("ok  going live requires a typed confirmation")


def test_go_live_without_a_key_is_refused():
    """The typed phrase alone is not enough -- there must be a key set."""
    d = Dashboard(with_key=False)
    try:
        status, body = d.post(
            "/api/mode", {"dryRun": False, "confirm": "GO LIVE"}
        )
        assert status == 422, (status, body)
        assert "PRIVATE_KEY_BASE58" in body, body
        assert json.loads(d.request("/api/state")[1])["mode"] == "dry_run"
    finally:
        d.close()
    print("ok  going live without a configured key is refused")


def test_going_dry_needs_no_phrase():
    """Dry run is the safe direction, so it is a single deliberate click.

    Both directions are asserted against `.env` as well as the reported
    mode, because the two can disagree: an inverted write here would leave
    the bot reporting one mode while persisting the other.
    """

    def persisted_mode():
        for line in d.env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DRY_RUN="):
                return line.split("=", 1)[1].strip()
        raise AssertionError("DRY_RUN missing from .env")

    d = Dashboard()
    try:
        assert persisted_mode() == "true"

        # Flip live first (needs the phrase + a key, both present).
        status, body = d.post("/api/mode", {"dryRun": False, "confirm": "GO LIVE"})
        assert status == 200, (status, body)
        assert json.loads(body)["mode"] == "live", body
        assert persisted_mode() == "false", ".env disagrees with the live mode"

        status, body = d.post("/api/mode", {"dryRun": True})
        assert status == 200, (status, body)
        assert json.loads(body)["mode"] == "dry_run", body
        assert persisted_mode() == "true", ".env disagrees with the dry-run mode"
    finally:
        d.close()
    print("ok  switching back to dry run needs no phrase")


def test_kill_switch_lifecycle():
    d = Dashboard()
    try:
        status, body = d.post("/api/kill", {"engage": True})
        assert status == 200, (status, body)
        assert d.engine.cfg.kill_switch_file.exists(), "kill file not created"

        # Releasing reopens the gate to live entries, so it needs the phrase.
        status, body = d.post("/api/kill", {"engage": False})
        assert status == 422, (status, body)
        assert d.engine.cfg.kill_switch_file.exists(), "released too easily"

        status, body = d.post(
            "/api/kill", {"engage": False, "confirm": "resume"}
        )
        assert status == 200, (status, body)
        assert d.engine.cfg.kill_switch_file.exists() is False
    finally:
        d.close()
    print("ok  kill switch engages freely and releases deliberately")


def test_settings_change_is_persisted_and_applied():
    d = Dashboard()
    try:
        status, body = d.post("/api/settings", {"changes": {"EMA_FAST": "12"}})
        assert status == 200, (status, body)
        assert json.loads(body)["values"]["EMA_FAST"] == "12"

        # Applied to the running engine, not just written to disk.
        assert d.engine.cfg.ema_fast == 12, d.engine.cfg.ema_fast
        assert "EMA_FAST=12" in d.env_path.read_text(encoding="utf-8")

        # Hand-written comments survive the edit.
        assert "# test fixture" in d.env_path.read_text(encoding="utf-8")
    finally:
        d.close()
    print("ok  settings changes are persisted and applied")


def test_invalid_settings_are_rolled_back():
    d = Dashboard()
    try:
        before = d.env_path.read_text(encoding="utf-8")

        # EMA_FAST >= EMA_SLOW is rejected by the same validation startup uses.
        status, body = d.post(
            "/api/settings", {"changes": {"EMA_FAST": "999"}}
        )
        assert status == 422, (status, body)
        assert d.engine.cfg.ema_fast == 9, d.engine.cfg.ema_fast
        assert d.env_path.read_text(encoding="utf-8") == before, (
            ".env was not restored after a rejected change"
        )
    finally:
        d.close()
    print("ok  invalid settings are rejected and rolled back")


def test_unknown_settings_keys_are_refused():
    """A hand-crafted request cannot smuggle arbitrary keys into .env."""
    d = Dashboard()
    try:
        before = d.env_path.read_text(encoding="utf-8")
        for key in ("PRIVATE_KEY_BASE58", "RPC_URL", "KILL_SWITCH_FILE", "NOPE"):
            status, body = d.post("/api/settings", {"changes": {key: "x"}})
            assert status == 422, (key, status, body)
        assert d.env_path.read_text(encoding="utf-8") == before, (
            "an unknown key was written into .env"
        )
    finally:
        d.close()
    print("ok  unknown settings keys are refused")


def test_no_response_ever_contains_the_private_key():
    d = Dashboard(with_key=True)
    try:
        assert d.engine.cfg.private_key_base58 == FAKE_PRIVATE_KEY

        responses = [
            d.request("/")[1],
            d.request("/api/state")[1],
            d.request("/api/schema")[1],
            d.request("/api/trades")[1],
            d.request("/api/log")[1],
            d.post("/api/settings", {"changes": {"EMA_FAST": "11"}})[1],
        ]
        for index, body in enumerate(responses):
            assert FAKE_PRIVATE_KEY not in body, (
                f"response {index} leaked PRIVATE_KEY_BASE58"
            )
    finally:
        d.close()
    print("ok  no response ever contains the private key")


def test_static_serving_refuses_path_traversal():
    d = Dashboard()
    try:
        for path in (
            "/static/../config.py",
            "/static/..%2Fconfig.py",
            "/static/..%5Cconfig.py",
            "/static/%2e%2e/config.py",
        ):
            status, _ = d.request(path)
            assert status in (404, 403), (path, status)
    finally:
        d.close()
    print("ok  static serving refuses path traversal")


def test_binds_loopback_by_default():
    """The default must never expose the dashboard to the LAN."""
    d = Dashboard()
    try:
        assert d.engine.cfg.ui_host == "127.0.0.1", d.engine.cfg.ui_host
        # Connecting to the machine's non-loopback side should not work; the
        # server socket is bound to 127.0.0.1 specifically.
        assert d.server._httpd.server_address[0] == "127.0.0.1"
    finally:
        d.close()
    print("ok  dashboard binds loopback by default")


def test_candles_and_trending_routes():
    """Chart endpoints are wired, shaped, and bounded.

    The market calls are stubbed: these tests must never reach the real
    GeckoTerminal API, and a route that silently did would show up as a
    slow, flaky suite rather than a failure.
    """
    ohlcv = [
        {
            "time": 1_000_000 + i * 900,
            "open": 100.0 + i, "high": 101.0 + i,
            "low": 99.0 + i, "close": 100.5 + i, "volume": 1234.0,
        }
        for i in range(40)
    ]
    trending = [{
        "pool": "solana_ABC", "name": "TEST / USDC",
        "baseSymbol": "TEST", "quoteSymbol": "USDC", "baseMint": "ABC",
        "priceUsd": 0.5, "volume24h": 900_000.0, "marketCap": 5_000_000.0,
    }]

    original_ohlcv = MarketData.fetch_ohlcv
    original_trending = MarketData.fetch_trending
    MarketData.fetch_ohlcv = lambda self, limit=300: ohlcv[:limit]
    MarketData.fetch_trending = lambda self, limit=30: trending[:limit]

    d = Dashboard()
    try:
        status, body = d.request("/api/candles?limit=40")
        assert status == 200, body
        payload = json.loads(body)
        assert payload["ok"] is True
        assert len(payload["candles"]) == 40
        # Ascending, oldest first: a reversed feed would draw a mirrored chart.
        assert payload["candles"][0]["time"] < payload["candles"][-1]["time"]

        # The whole point of the overlay is that it lines up with the bars.
        n = len(payload["candles"])
        for name, pack in payload["series"].items():
            assert pack["start"] + len(pack["values"]) == n, (name, pack)

        status, body = d.request("/api/trending?limit=5")
        assert status == 200, body
        payload = json.loads(body)
        assert payload["ok"] is True
        assert [t["baseSymbol"] for t in payload["tokens"]] == ["TEST"]

        # Limits are clamped server-side, so a hand-edited URL cannot ask
        # the rate-limited upstream API for an enormous page.
        assert d.request("/api/candles?limit=5")[0] == 400
        assert d.request("/api/candles?limit=99999")[0] == 400
        assert d.request("/api/trending?limit=0")[0] == 400
    finally:
        d.close()
        MarketData.fetch_ohlcv = original_ohlcv
        MarketData.fetch_trending = original_trending
    print("ok  /api/candles and /api/trending are wired, shaped and bounded")


def main() -> int:
    tests = [
        test_dashboard_serves_the_page,
        test_state_endpoint_reports_a_flat_bot,
        test_schema_lists_only_editable_settings,
        test_post_rejects_non_json_bodies,
        test_cross_origin_post_is_refused,
        test_go_live_requires_a_typed_confirmation,
        test_go_live_without_a_key_is_refused,
        test_going_dry_needs_no_phrase,
        test_kill_switch_lifecycle,
        test_settings_change_is_persisted_and_applied,
        test_invalid_settings_are_rolled_back,
        test_unknown_settings_keys_are_refused,
        test_no_response_ever_contains_the_private_key,
        test_static_serving_refuses_path_traversal,
        test_binds_loopback_by_default,
        test_candles_and_trending_routes,
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
