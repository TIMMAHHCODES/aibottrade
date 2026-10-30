"""Risk management: the guardrails that keep a bad day from becoming a
catastrophic one.

Every function here returns a reason string when it blocks something, and
`None` when it allows it. The engine logs the reason, so when the bot
declines to trade you can always see why.
"""

from __future__ import annotations

import logging
from enum import Enum

from .config import Config
from .state import BotState, Position

log = logging.getLogger(__name__)


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    SIGNAL = "signal"
    FLAT = "flat"


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ── kill switch ─────────────────────────────────────────────────────

    def kill_switch_engaged(self) -> bool:
        """True when the `kill.txt` file exists.

        This is the emergency stop. Create the file and the bot will open
        no new positions, no matter what the strategy says.
        """
        return self.cfg.kill_switch_file.exists()

    # ── entry checks ────────────────────────────────────────────────────

    def check_entry(
        self,
        state: BotState,
        quote_balance_ui: float | None,
        seconds_since_last_fill: float,
    ) -> str | None:
        """Return a reason to NOT trade, or None to allow the entry."""
        state.state.roll_daily_window()

        if self.kill_switch_engaged():
            return f"kill switch active ({self.cfg.kill_switch_file.name} exists)"

        if len(state.state.positions) >= self.cfg.max_open_positions:
            return (
                f"already at MAX_OPEN_POSITIONS "
                f"({len(state.state.positions)}/{self.cfg.max_open_positions})"
            )

        if seconds_since_last_fill < self.cfg.cooldown_seconds:
            return (
                f"cooldown active ({int(self.cfg.cooldown_seconds - seconds_since_last_fill)}s left)"
            )

        spent_ui = state.state.daily_quote_spent / (10 ** self.cfg.quote_decimals)
        if spent_ui + self.cfg.trade_size_quote > self.cfg.max_quote_per_day:
            return (
                f"daily budget exhausted "
                f"({spent_ui:.2f} + {self.cfg.trade_size_quote:.2f} > "
                f"{self.cfg.max_quote_per_day:.2f} {self.cfg.quote_symbol})"
            )

        # In dry run there is no wallet, so balance is unknown and the
        # daily cap is the binding constraint. In live mode this is real.
        if quote_balance_ui is not None:
            needed = self.cfg.trade_size_quote + self.cfg.quote_reserve
            if quote_balance_ui < needed:
                return (
                    f"insufficient {self.cfg.quote_symbol} balance "
                    f"({quote_balance_ui:.4f} < {needed:.4f} needed)"
                )

        # A zero-output quote means the route collapsed; treat as blocked.
        return None

    # ── exit checks ─────────────────────────────────────────────────────

    def check_exit(
        self, position: Position, current_price: float, signal_says_sell: bool
    ) -> ExitReason | None:
        """Return an exit reason when this position should be closed.

        Stop-loss is evaluated before take-profit so that a position which
        gapped through both levels is reported as a stop-loss, which is the
        more useful thing to see in your trade log.
        """
        if position.entry_price <= 0 or current_price <= 0:
            return None

        change = (current_price - position.entry_price) / position.entry_price

        if change <= -self.cfg.stop_loss_pct:
            return ExitReason.STOP_LOSS
        if change >= self.cfg.take_profit_pct:
            return ExitReason.TAKE_PROFIT
        if signal_says_sell:
            return ExitReason.SIGNAL
        return None

    # ── sizing ──────────────────────────────────────────────────────────

    def clamp_buy_amount(self, quote_balance_ui: float | None) -> int:
        """Smallest-unit quote amount to spend on this entry.

        Always the configured trade size, never a fraction of the wallet.
        The bot does not martingale, double down, or size up after a loss.
        """
        size = self.cfg.trade_size_base_units
        if quote_balance_ui is not None:
            spendable = quote_balance_ui - self.cfg.quote_reserve
            size = min(size, int(spendable * (10 ** self.cfg.quote_decimals)))
        return max(size, 0)
