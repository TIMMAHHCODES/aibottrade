"""The trading engine: one loop that reads data, decides, acts, and records.

Order of operations each cycle matters:
  1. Pick up any pending config change.
  2. Evaluate EXITS first, against the live price. Reducing exposure never
     waits behind opening a new position.
  3. Evaluate the ENTRY signal from closed candles.
  4. Persist state and log.

Threading: the dashboard runs in other threads and only ever *reads* (via
`snapshot()` / `current_price()`) or writes `.env`. Config is always applied
by this thread -- in `maybe_reload_config()` -- so components are never
swapped underneath an in-flight cycle.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import Config, ConfigError, load_config
from .data import DataError, MarketData
from .jupiter import Fill, Jupiter, JupiterError
from .risk import ExitReason, RiskManager
from .settings import apply_changes, current_values, set_dry_run
from .state import StateStore
from .strategy import Signal, evaluate, indicator_series
from .wallet import Wallet, WalletError

log = logging.getLogger(__name__)

# Returned when there is no trade log yet, so callers never have to branch
# on None for every field.
_EMPTY_STATS = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "winRate": None,
    "totalPnl": 0.0,
    "avgPnl": None,
    "bestPnl": None,
    "worstPnl": None,
}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = StateStore(cfg.working_dir)
        self.risk = RiskManager(cfg)
        self.market = MarketData(cfg)

        self.wallet: Wallet | None = None
        self._wallet_key = ""
        if not cfg.dry_run:
            self.wallet = Wallet(cfg.private_key_base58, cfg.rpc_url)
            self._wallet_key = cfg.private_key_base58

        self.jupiter = Jupiter(cfg, self.wallet)
        self.last_fill_at = 0.0

        # ── dashboard / observability state ─────────────────────────────
        # Guards only these scalar fields (never the network calls, so the
        # UI is not blocked while a request is in flight).
        self._lock = threading.RLock()
        self._price_lock = threading.Lock()
        self._price_cache: tuple[float, float, str] | None = None

        # Chart/discovery caches. Separate from the price cache because the
        # dashboard re-renders on every poll while GeckoTerminal's free tier
        # only allows ~30 requests a minute -- without these the chart alone
        # would exhaust the budget.
        self._candle_cache: dict[int, tuple[dict, float]] = {}
        self._trending_cache: tuple[list[dict], float] | None = None

        # Populated by the dashboard's background refresher, so /api/state
        # can answer from cache instead of blocking on a network call.
        self._price_thread: threading.Thread | None = None
        self._price_stop = threading.Event()
        self._price_interval = 30.0

        self.started_at: float = time.time()
        self.running = False
        self.cycle_count = 0
        self.last_cycle_at: float | None = None
        self.last_cycle_duration = 0.0
        self.last_status = "not started"
        self.last_error: str | None = None
        self.last_signal: Signal | None = None
        self.last_snapshot = None
        self.last_price: float | None = None
        self.next_cycle_at: float | None = None
        self.config_error: str | None = None

        # Set by the dashboard when `.env` changes; the loop wakes on it so
        # settings apply promptly without firing an early trading cycle.
        self.reload_requested = threading.Event()
        self._env_mtime = self._read_env_mtime()

    # ── lifecycle ───────────────────────────────────────────────────────

    def close(self) -> None:
        self.stop_price_refresher()
        self.market.close()
        self.jupiter.close()
        if self.wallet is not None:
            self.wallet.close()

    def _read_env_mtime(self) -> int | None:
        try:
            return (self.cfg.working_dir / ".env").stat().st_mtime_ns
        except OSError:
            return None

    # ── balance helpers ─────────────────────────────────────────────────

    def _quote_balance_ui(self) -> float | None:
        # In dry run there is no wallet to ask; "unknown" is the honest
        # answer, and it lets the daily cap act as the binding constraint.
        if self.cfg.dry_run or self.wallet is None:
            return None
        raw = self.wallet.get_token_balance(self.cfg.quote_mint)
        return raw / (10 ** self.cfg.quote_decimals)

    def _base_balance_units(self) -> int | None:
        """Base units held in the wallet, or None in dry run.

        None is meaningfully different from 0: dry run has no wallet, so the
        bot must fall back to its own bookkeeping rather than conclude it
        holds nothing and refuse to exit.
        """
        if self.cfg.dry_run or self.wallet is None:
            return None
        return self.wallet.get_token_balance(self.cfg.base_mint)

    def _units_to_sell(self, position) -> int:
        """Base units to sell when closing `position`.

        Never sell more than actually held: dust, fee deductions, and manual
        trades can all make the wallet disagree with the bot's records.
        """
        held = self._base_balance_units()
        if held is None:
            return position.base_amount
        return min(position.base_amount, held)

    # ── one cycle ───────────────────────────────────────────────────────

    def run_once(self) -> str:
        """Run a single decision cycle. Returns a short status label."""
        self.maybe_reload_config()
        started = time.time()
        try:
            status = self._cycle()
            self._record_cycle(status, None, started)
            return status
        except Exception as exc:
            self._record_cycle(f"error: {exc}", str(exc), started)
            raise

    def _cycle(self) -> str:
        closes = self.market.fetch_closes()
        signal, snapshot = evaluate(closes, self.cfg)
        price = self.market.fetch_price()

        mode = "DRY RUN" if self.cfg.dry_run else "LIVE"
        log.info("[%s] %s | %s", mode, snapshot.describe(), signal.value)

        with self._lock:
            self.last_signal = signal
            self.last_snapshot = snapshot
            self.last_price = price
            self._price_cache = (
                price, time.time(), datetime.now(timezone.utc).isoformat()
            )

        # Exits always run, even with the kill switch engaged: closing a
        # position shrinks risk, which is exactly what you want in a panic.
        self._process_exits(price, signal is Signal.SELL)

        # Entries are gated by the kill switch inside check_entry.
        status = "hold"
        if signal is Signal.BUY:
            status = self._process_entry(price, snapshot.rsi)
        return status

    def _record_cycle(self, status: str, error: str | None, started: float) -> None:
        with self._lock:
            self.cycle_count += 1
            self.last_cycle_at = started
            self.last_cycle_duration = time.time() - started
            self.last_status = status
            self.last_error = error

    # ── exits ───────────────────────────────────────────────────────────

    def _process_exits(self, price: float, signal_says_sell: bool) -> None:
        # Snapshot the list: we mutate it while iterating.
        for position in self.store.positions_snapshot():
            reason = self.risk.check_exit(position, price, signal_says_sell)
            if reason is None:
                continue

            # Never sell more than we actually hold; dust and manual trades
            # can make the wallet disagree with the bot's bookkeeping.
            units_to_sell = self._units_to_sell(position)
            if units_to_sell <= 0 and not self.cfg.dry_run:
                log.warning(
                    "Position %s wants to exit (%s) but wallet holds 0 %s. "
                    "Closing the position without a trade.",
                    position.id, reason.value, self.cfg.base_symbol,
                )
                self.store.remove_position(position.id)
                continue

            log.info(
                "EXIT %s: %s at %.6g %s/units",
                position.id[:8], reason.value, price, self.cfg.quote_symbol,
            )
            try:
                fill = self._swap(self.cfg.base_mint, self.cfg.quote_mint, units_to_sell)
            except (JupiterError, WalletError, DataError) as exc:
                log.error("Exit swap failed, will retry next cycle: %s", exc)
                continue

            self._close_with_fill(position, fill, reason, price)

    def _close_with_fill(
        self, position, fill: Fill, reason: ExitReason, price_at_exit: float
    ) -> None:
        quote_back_ui = fill.out_amount / (10 ** self.cfg.quote_decimals)
        cost_ui = position.quote_spent / (10 ** self.cfg.quote_decimals)
        pnl_ui = quote_back_ui - cost_ui
        pnl_pct = (pnl_ui / cost_ui * 100) if cost_ui else 0.0

        log.info(
            "CLOSED %s [%s] cost=%.4f returned=%.4f pnl=%+.4f %s (%+.2f%%)",
            position.id[:8], reason.value, cost_ui, quote_back_ui,
            pnl_ui, self.cfg.quote_symbol, pnl_pct,
        )

        self.store.remove_position(position.id)
        self.store.log_trade({
            "side": "SELL",
            "exit_reason": reason.value,
            "position_id": position.id,
            "mode": "dry_run" if fill.dry_run else "live",
            "base_symbol": self.cfg.base_symbol,
            "quote_symbol": self.cfg.quote_symbol,
            "entry_price": position.entry_price,
            "exit_price": price_at_exit,
            "quote_spent": cost_ui,
            "quote_received": quote_back_ui,
            "pnl_quote": round(pnl_ui, 8),
            "pnl_pct": round(pnl_pct, 4),
            "signature": fill.signature,
        })

    # ── entries ─────────────────────────────────────────────────────────

    def _process_entry(self, price: float, rsi_value: float) -> str:
        balance_ui = self._quote_balance_ui()
        elapsed = time.time() - self.last_fill_at if self.last_fill_at else 1e9

        blocked = self.risk.check_entry(self.store, balance_ui, elapsed)
        if blocked:
            log.info("BUY signal ignored: %s", blocked)
            return f"blocked: {blocked}"

        amount = self.risk.clamp_buy_amount(balance_ui)
        if amount <= 0:
            log.info("BUY signal ignored: nothing left to spend after reserve.")
            return "blocked: nothing to spend"

        log.info(
            "ENTRY: swapping %d %s units (~%.4f %s) into %s",
            amount, self.cfg.quote_symbol,
            amount / (10 ** self.cfg.quote_decimals),
            self.cfg.quote_symbol, self.cfg.base_symbol,
        )

        try:
            fill = self._swap(self.cfg.quote_mint, self.cfg.base_mint, amount)
        except (JupiterError, WalletError, DataError) as exc:
            log.error("Entry swap failed: %s", exc)
            return f"error: {exc}"

        self.last_fill_at = time.time()

        quote_in_ui = fill.in_amount / (10 ** self.cfg.quote_decimals)
        base_out_ui = fill.out_amount / (10 ** self.cfg.base_decimals)
        if base_out_ui <= 0:
            log.error("Swap returned zero %s; not opening a position.", self.cfg.base_symbol)
            return "error: zero output"

        entry_price = quote_in_ui / base_out_ui
        position = self.store.open_position(
            base_amount=fill.out_amount,
            quote_spent=fill.in_amount,
            entry_price=entry_price,
            signature=fill.signature,
        )

        log.info(
            "OPENED %s: %.6f %s @ %.6g %s/%s%s",
            position.id[:8], base_out_ui, self.cfg.base_symbol, entry_price,
            self.cfg.quote_symbol, self.cfg.base_symbol,
            " [simulated]" if fill.dry_run else "",
        )
        self.store.log_trade({
            "side": "BUY",
            "position_id": position.id,
            "mode": "dry_run" if fill.dry_run else "live",
            "base_symbol": self.cfg.base_symbol,
            "quote_symbol": self.cfg.quote_symbol,
            "quote_spent": quote_in_ui,
            "base_received": base_out_ui,
            "entry_price": entry_price,
            "rsi_at_entry": round(rsi_value, 4),
            "price_at_signal": price,
            "signature": fill.signature,
        })
        return "bought"

    # ── plumbing ────────────────────────────────────────────────────────

    def _swap(self, input_mint: str, output_mint: str, amount: int) -> Fill:
        quote = self.jupiter.get_quote(input_mint, output_mint, amount)

        # Price impact is a direct cost. Refuse routes that are obviously bad
        # rather than paying them silently.
        if quote.price_impact_pct > self.cfg.slippage_bps / 100.0:
            raise JupiterError(
                f"Price impact {quote.price_impact_pct:.4f}% exceeds slippage budget "
                f"{self.cfg.slippage_bps / 100.0:.4f}%."
            )

        return self.jupiter.execute(quote, dry_run=self.cfg.dry_run)

    # ── configuration (hot reload) ──────────────────────────────────────

    def maybe_reload_config(self) -> str | None:
        """Apply a changed `.env`. Only ever called from the cycle thread.

        Returns "reloaded", a rejection reason, or None if nothing changed.
        """
        env_path = self.cfg.working_dir / ".env"
        mtime = self._read_env_mtime()
        if mtime is None or mtime == self._env_mtime:
            return None
        self._env_mtime = mtime

        try:
            new_cfg = load_config(self.cfg.working_dir)
            new_wallet = self._resolve_wallet(new_cfg)
        except (ConfigError, WalletError) as exc:
            # Keep running on the last known-good configuration rather than
            # taking the bot down over a typo.
            self.config_error = str(exc)
            log.error("Config reload rejected; keeping current settings: %s", exc)
            return f"rejected: {exc}"

        self._apply_config(new_cfg, new_wallet)
        self.config_error = None
        log.info(
            "Configuration reloaded: %s | %sm candles | EMA %d/%d | "
            "%.4f %s per trade | cap %.4f",
            "DRY RUN" if new_cfg.dry_run else "LIVE",
            new_cfg.timeframe_minutes, new_cfg.ema_fast, new_cfg.ema_slow,
            new_cfg.trade_size_quote, new_cfg.quote_symbol,
            new_cfg.max_quote_per_day,
        )
        return "reloaded"

    def _resolve_wallet(self, cfg: Config) -> Wallet | None:
        # Mirror __init__: dry run has no wallet at all. Building one here
        # would make a stray or malformed key in .env break dry-run reloads
        # for no reason -- the key is never used until you go live.
        if cfg.dry_run or not cfg.private_key_base58:
            return None
        if self.wallet is not None and cfg.private_key_base58 == self._wallet_key:
            return self.wallet
        # Raises WalletError on a bad key; the caller keeps the old config.
        return Wallet(cfg.private_key_base58, cfg.rpc_url)

    def _apply_config(self, new_cfg: Config, new_wallet: Wallet | None) -> None:
        old_market, old_jupiter, old_wallet = self.market, self.jupiter, self.wallet

        self.cfg = new_cfg
        self.risk = RiskManager(new_cfg)
        self.market = MarketData(new_cfg)
        self.wallet = new_wallet
        self._wallet_key = new_cfg.private_key_base58
        self.jupiter = Jupiter(new_cfg, new_wallet)
        self._price_cache = None  # the pool or pair may have changed
        self._candle_cache.clear()
        self._trending_cache = None

        # Safe to close the previous generation: this runs on the cycle
        # thread, so nothing else is using them.
        for obj in (old_market, old_jupiter, old_wallet):
            if obj is None or obj is new_wallet:
                continue
            try:
                obj.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    # ── dashboard control surface ───────────────────────────────────────

    def request_reload(self) -> None:
        """Wake the main loop so a `.env` edit applies without waiting a candle."""
        self.reload_requested.set()

    def set_kill_switch(self, engage: bool) -> bool:
        """Engage or release the emergency stop. Returns the new state."""
        path = self.cfg.kill_switch_file
        if engage:
            path.write_text(
                f"Engaged at {datetime.now(timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )
            log.warning("KILL SWITCH ENGAGED. No new positions will be opened.")
        else:
            if path.exists():
                path.unlink()
            log.warning("Kill switch released. Entries are allowed again.")
        return path.exists()

    def set_mode(self, dry_run: bool) -> None:
        """Switch dry run <-> live. Validates, persists, and applies."""
        previous = self.cfg
        new_cfg = set_dry_run(previous, dry_run)
        # Compare the value, not the object: apply functions always return a
        # fresh Config, so an identity check would report a change that did
        # not happen.
        if new_cfg.dry_run == previous.dry_run:
            return
        log.warning(
            "MODE CHANGED -> %s. Lots opened in dry run will be settled "
            "against the real wallet balance, if any.",
            "DRY RUN" if new_cfg.dry_run else "LIVE TRADING",
        )
        self._apply_or_schedule()

    def save_settings(self, changes: dict[str, str]) -> None:
        """Persist dashboard edits and apply them."""
        new_cfg = apply_changes(self.cfg, changes)
        if new_cfg is not self.cfg:
            self._apply_or_schedule()

    def _apply_or_schedule(self) -> None:
        """Apply a fresh `.env` now if idle, else hand it to the loop.

        When the loop is running it owns all component swaps, so we only
        nudge it awake -- it reloads at a safe point and still waits for the
        real candle boundary before trading.
        """
        if self.running:
            self.request_reload()
        else:
            self.maybe_reload_config()

    # ── main loop ───────────────────────────────────────────────────────

    def seconds_until_next_candle(self) -> float:
        """Sleep until the next candle closes, plus a small buffer.

        GeckoTerminal needs a moment to publish the finished candle; acting
        instantly on the boundary usually reads the candle before it exists.
        """
        period = self.cfg.timeframe_minutes * 60
        now = time.time()
        return max(5.0, period - (now % period) + 15.0)

    def run_forever(self) -> None:
        mode = "DRY RUN" if self.cfg.dry_run else "LIVE"
        log.info("=" * 68)
        log.info("Starting %s | %s/%s | %s candles | EMA %d/%d | RSI %d",
                 mode, self.cfg.base_symbol, self.cfg.quote_symbol,
                 f"{self.cfg.timeframe_minutes}m", self.cfg.ema_fast,
                 self.cfg.ema_slow, self.cfg.rsi_period)
        log.info("Per trade %.4f %s | daily cap %.4f %s | max %d positions",
                 self.cfg.trade_size_quote, self.cfg.quote_symbol,
                 self.cfg.max_quote_per_day, self.cfg.quote_symbol,
                 self.cfg.max_open_positions)
        if self.cfg.dry_run:
            log.info("DRY RUN is ON. No transactions will be signed or sent.")
        log.info("Kill switch: create '%s' to block new entries.",
                 self.cfg.kill_switch_file.name)
        log.info("=" * 68)

        self.running = True
        try:
            while True:
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    log.info("Interrupted. Stopping.")
                    return
                except (DataError, JupiterError, WalletError) as exc:
                    # Transient infrastructure failure. Log it and keep going;
                    # a network blip should not kill an otherwise healthy bot.
                    log.error("Cycle failed (%s); retrying next candle.", exc)
                except Exception:
                    log.exception("Unexpected error in cycle. Continuing.")

                self._wait_for_next_candle()
        finally:
            self.running = False
            self.next_cycle_at = None

    def _wait_for_next_candle(self) -> None:
        """Sleep until the candle closes, waking early on a config change.

        Waking early applies settings promptly but deliberately does NOT run
        a cycle: trading off a still-forming candle is exactly what we are
        waiting to avoid. It re-computes the remaining wait instead.
        """
        while True:
            wait = self.seconds_until_next_candle()
            self.next_cycle_at = time.time() + wait
            log.info("Next candle in %.0fs.", wait)

            if self.reload_requested.wait(timeout=wait):
                self.reload_requested.clear()
                self.maybe_reload_config()
                continue

            self.next_cycle_at = None
            return

    # ── diagnostics ─────────────────────────────────────────────────────

    def check_connectivity(self) -> bool:
        """Verify every external dependency before you trust the bot."""
        ok = True
        log.info("Checking configuration and connectivity...")

        try:
            pool = self.market.resolve_pool()
            log.info("  [ok] pool resolved: %s", pool)
        except DataError as exc:
            log.error("  [FAIL] pool resolution: %s", exc)
            ok = False

        try:
            closes = self.market.fetch_closes(limit=100)
            signal, snapshot = evaluate(closes, self.cfg)
            log.info("  [ok] %d candles fetched", len(closes))
            log.info("  [ok] %s", snapshot.describe())
            log.info("  [ok] current signal: %s", signal.value)
            with self._lock:
                self.last_signal = signal
                self.last_snapshot = snapshot
        except (DataError, ValueError) as exc:
            log.error("  [FAIL] candle fetch / indicators: %s", exc)
            ok = False

        try:
            price = self.market.fetch_price()
            log.info("  [ok] %s price = %.6g %s",
                     self.cfg.base_symbol, price, self.cfg.quote_symbol)
            with self._lock:
                self.last_price = price
        except DataError as exc:
            log.error("  [FAIL] price fetch: %s", exc)
            ok = False

        # A real quote, read-only, proving Jupiter can route this pair.
        amount = self.cfg.trade_size_base_units
        try:
            q = self.jupiter.get_quote(
                self.cfg.quote_mint, self.cfg.base_mint, amount
            )
            log.info("  [ok] Jupiter quote: %d -> %d units (impact %.4f%%)",
                     q.in_amount, q.out_amount, q.price_impact_pct)
        except JupiterError as exc:
            log.error("  [FAIL] Jupiter quote: %s", exc)
            ok = False

        if self.wallet is not None:
            try:
                sol = self.wallet.get_sol_lamports() / 1e9
                log.info("  [ok] wallet %s holds %.6f SOL for fees",
                         self.wallet.pubkey, sol)
                if sol < 0.001:
                    log.warning("  [warn] less than 0.001 SOL for fees; "
                                "transactions will likely fail.")
            except WalletError as exc:
                log.error("  [FAIL] wallet RPC: %s", exc)
                ok = False

            # Confirm the quote mint is held at all.
            try:
                bal = self._quote_balance_ui()
                log.info("  [ok] %s balance = %.4f",
                         self.cfg.quote_symbol, bal if bal is not None else 0.0)
                if bal is not None and bal < self.cfg.trade_size_quote:
                    log.warning("  [warn] balance is below one trade size.")
            except WalletError as exc:
                log.error("  [FAIL] token balance: %s", exc)
                ok = False
        else:
            log.info("  [ok] dry run: no wallet required")

        log.info("Connectivity check %s.", "PASSED" if ok else "FAILED")
        return ok

    def current_price(
        self, max_age: float = 30.0, *, wait: bool = True
    ) -> tuple[float | None, str | None]:
        """(price, iso timestamp), refreshing at most every `max_age` seconds.

        Serialised through `_price_lock` so a dashboard polling every couple
        of seconds cannot stampede GeckoTerminal's ~30 req/min limit. On a
        failed fetch it falls back to the last known price rather than
        blanking the UI.

        `wait=False` never touches the network: it returns whatever is cached
        (even stale) and lets the background refresher catch up. Dashboard
        handlers pass False, because a cold cache plus a flaky market API
        would otherwise leave a request sitting in retry backoff for the
        best part of a minute.

        Any exception is swallowed, not just DataError: reloading config
        swaps in a new HTTP client, and an in-flight call on the old one
        raises RuntimeError. That is a reason to show a slightly stale
        price, never a reason to 500 the dashboard.
        """
        now = time.time()
        if self._price_cache and now - self._price_cache[1] < max_age:
            return self._price_cache[0], self._price_cache[2]

        if not wait:
            if self._price_cache:
                return self._price_cache[0], self._price_cache[2]
            return None, None

        with self._price_lock:
            # Re-check: another thread may have refreshed while we waited.
            now = time.time()
            if self._price_cache and now - self._price_cache[1] < max_age:
                return self._price_cache[0], self._price_cache[2]
            try:
                price = self.market.fetch_price()
            except Exception as exc:  # noqa: BLE001
                log.warning("Price refresh failed: %s", exc)
                if self._price_cache:
                    return self._price_cache[0], self._price_cache[2]
                return None, str(exc)
            stamp = datetime.now(timezone.utc).isoformat()
            self._price_cache = (price, time.time(), stamp)
            return price, stamp

    # ── background price refresh ────────────────────────────────────────

    def start_price_refresher(self, interval: float = 30.0) -> None:
        """Keep the cached price warm while the dashboard is open.

        The dashboard polls every couple of seconds but the trading cycle
        only touches the price once per candle, so without this the
        "current price" and every open position's PnL would sit still for
        the whole timeframe. Started only alongside the dashboard, so a
        headless bot makes no extra API calls.
        """
        if self._price_thread is not None:
            return
        self._price_interval = max(5.0, interval)
        self._price_stop.clear()
        self._price_thread = threading.Thread(
            target=self._price_loop, name="price-refresh", daemon=True
        )
        self._price_thread.start()

    def stop_price_refresher(self) -> None:
        self._price_stop.set()
        thread, self._price_thread = self._price_thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _price_loop(self) -> None:
        while not self._price_stop.is_set():
            try:
                self.current_price(max_age=0.0)
            except Exception:  # noqa: BLE001 - a background poll must never die
                log.debug("Background price refresh failed.", exc_info=True)
            if self._price_stop.wait(self._price_interval):
                return

    def _realized_pnl(self) -> float:
        """Sum of realised PnL from the trade log, cached on file size/mtime.

        Reading and parsing the whole log on every dashboard poll would get
        wasteful as it grows; one stat() is not.
        """
        return self._trade_stats()["totalPnl"]

    def _trade_stats(self) -> dict:
        """Closed-trade statistics, cached on the log's size and mtime.

        Shares one parse of trades.jsonl with `_realized_pnl`, so the
        dashboard reads the file once per change rather than once per field.
        """
        path = self.store.trades_path
        try:
            stat = path.stat()
        except OSError:
            return _EMPTY_STATS

        key = (stat.st_mtime_ns, stat.st_size)
        cached = getattr(self, "_stats_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]

        pnls: list[float] = []
        for trade in self.store.read_trades(limit=100_000):
            if trade.get("side") == "SELL":
                pnls.append(float(trade.get("pnl_quote") or 0.0))

        if not pnls:
            stats = dict(_EMPTY_STATS)
        else:
            wins = sum(1 for p in pnls if p > 0)
            stats = {
                "trades": len(pnls),
                "wins": wins,
                "losses": len(pnls) - wins,
                "winRate": wins / len(pnls) * 100.0,
                "totalPnl": sum(pnls),
                "avgPnl": sum(pnls) / len(pnls),
                "bestPnl": max(pnls),
                "worstPnl": min(pnls),
            }

        self._stats_cache = (key, stats)
        return stats

    # ── chart and discovery data ────────────────────────────────────────

    # The chart re-renders on every dashboard poll, so these are cached.
    # GeckoTerminal's free tier allows roughly 30 requests a minute; without
    # a cache the chart alone would spend the whole budget.
    CANDLE_TTL = 60.0
    TRENDING_TTL = 180.0

    def candles(self, limit: int = 300) -> dict:
        """OHLCV plus indicator overlays for the dashboard chart.

        On a fetch failure it serves the last good payload instead of
        blanking the chart: a flaky market API is a reason to show a stale
        line, not to make the dashboard look broken.
        """
        limit = max(30, min(int(limit), 1000))
        now = time.time()
        cached = self._candle_cache.get(limit)
        if cached and now - cached[1] < self.CANDLE_TTL:
            return cached[0]

        try:
            rows = self.market.fetch_ohlcv(limit=limit)
        except Exception as exc:  # noqa: BLE001 - a chart must never 500
            if cached:
                log.warning("Chart refresh failed, serving cached candles: %s", exc)
                return cached[0]
            raise

        closes = [row["close"] for row in rows]
        try:
            series = indicator_series(closes, self.cfg)
        except ValueError as exc:
            # Too little history for the indicators (a brand new pool, or
            # very slow settings). Draw the candles with no overlay rather
            # than refusing to render at all.
            log.debug("No indicator overlay available: %s", exc)
            series = {
                "emaFast": {"start": 0, "values": []},
                "emaSlow": {"start": 0, "values": []},
                "rsi": {"start": 0, "values": []},
            }

        payload = {
            "ok": True,
            "timeframe": self.cfg.timeframe_minutes,
            "base": self.cfg.base_symbol,
            "quote": self.cfg.quote_symbol,
            "candles": rows,
            "series": series,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._candle_cache[limit] = (payload, now)
        return payload

    def trending(self, limit: int = 30) -> dict:
        """Trending Solana pools. Read-only: nothing here can trigger a swap."""
        limit = max(5, min(int(limit), 100))
        now = time.time()
        cached = self._trending_cache
        if cached and now - cached[1] < self.TRENDING_TTL:
            return {
                "ok": True,
                "tokens": cached[0],
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
            }

        try:
            rows = self.market.fetch_trending(limit=limit)
        except Exception as exc:  # noqa: BLE001
            if cached:
                log.warning("Trending refresh failed, serving cached list: %s", exc)
                return {
                    "ok": True,
                    "tokens": cached[0],
                    "fetchedAt": datetime.now(timezone.utc).isoformat(),
                }
            raise

        self._trending_cache = (rows, now)
        return {
            "ok": True,
            "tokens": rows,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    def snapshot(self) -> dict:
        """Everything the dashboard renders, in one call.

        Never blocks on the network: price comes from cache (see
        `current_price`), so a slow market API degrades to a staler number
        rather than a hung request.
        """
        price, price_at = self.current_price(wait=False)

        with self._lock:
            signal = self.last_signal
            indicator = self.last_snapshot
            cycle = {
                "count": self.cycle_count,
                "lastAt": _iso(self.last_cycle_at),
                "duration": round(self.last_cycle_duration, 3),
                "status": self.last_status,
                "error": self.last_error,
                "nextAt": _iso(self.next_cycle_at),
                "nextIn": max(0, int(self.next_cycle_at - time.time()))
                if self.next_cycle_at else None,
            }
            started_at = self.started_at
            running = self.running
            last_price = self.last_price
            config_error = self.config_error

        positions = []
        unrealized = 0.0
        for p in self.store.positions_snapshot():
            pnl = p.unrealized_pnl_quote_units(
                price or last_price or p.entry_price,
                self.cfg.base_decimals,
                self.cfg.quote_decimals,
            )
            cost_ui = p.quote_spent / (10 ** self.cfg.quote_decimals)
            unrealized += pnl
            opened = _parse_iso(p.opened_at)
            age_s = (time.time() - opened) if opened else None
            positions.append({
                "id": p.id,
                "openedAt": p.opened_at,
                "ageSeconds": int(age_s) if age_s is not None else None,
                "baseAmount": p.base_amount / (10 ** self.cfg.base_decimals),
                "quoteSpent": cost_ui,
                "entryPrice": p.entry_price,
                "currentPrice": price or last_price,
                "value": (p.base_amount / (10 ** self.cfg.base_decimals))
                * (price or last_price or p.entry_price),
                "pnlQuote": pnl,
                "pnlPct": (pnl / cost_ui * 100) if cost_ui else 0.0,
                "takeProfitPrice": p.entry_price * (1 + self.cfg.take_profit_pct),
                "stopLossPrice": p.entry_price * (1 - self.cfg.stop_loss_pct),
                "signature": p.signature,
            })

        daily_date, daily_spent_units = self.store.daily_spend()
        daily_spent = daily_spent_units / (10 ** self.cfg.quote_decimals)

        return {
            "ok": True,
            "now": datetime.now(timezone.utc).isoformat(),
            "mode": "dry_run" if self.cfg.dry_run else "live",
            "running": running,
            "startedAt": _iso(started_at),
            "uiUrl": self.cfg.ui_url,
            "pair": {
                "base": self.cfg.base_symbol,
                "quote": self.cfg.quote_symbol,
                "baseMint": self.cfg.base_mint,
                "quoteMint": self.cfg.quote_mint,
                "timeframe": self.cfg.timeframe_minutes,
            },
            "price": price,
            "priceAt": price_at,
            "signal": signal.value if signal else None,
            "indicators": {
                "close": indicator.close,
                "emaFast": indicator.ema_fast,
                "emaSlow": indicator.ema_slow,
                "rsi": indicator.rsi,
                "crossedUp": indicator.crossed_up,
                "crossedDown": indicator.crossed_down,
            } if indicator else None,
            "cycle": cycle,
            "risk": {
                "killSwitch": self.risk.kill_switch_engaged(),
                "killSwitchFile": self.cfg.kill_switch_file.name,
                "dailyDate": daily_date,
                "dailySpent": daily_spent,
                "dailyCap": self.cfg.max_quote_per_day,
                "openCount": len(positions),
                "maxOpen": self.cfg.max_open_positions,
                "tradeSize": self.cfg.trade_size_quote,
                "cooldownSeconds": self.cfg.cooldown_seconds,
                "secondsSinceFill": int(time.time() - self.last_fill_at)
                if self.last_fill_at else None,
                "takeProfitPct": self.cfg.take_profit_pct * 100,
                "stopLossPct": self.cfg.stop_loss_pct * 100,
            },
            "positions": positions,
            "pnl": {
                "unrealized": unrealized,
                "realized": self._realized_pnl(),
            },
            "stats": self._trade_stats(),
            "settings": current_values(self.cfg),
            "configError": config_error,
        }


def _parse_iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None
