"""Persistent bot state: open positions, daily spend, and the trade log.

Amounts are stored as integers in each token's smallest unit, never floats,
so repeated save/load cycles cannot drift.

Files, all under the working directory:
  state.json     -> positions and the current UTC day's spend
  trades.jsonl   -> append-only audit log, one JSON object per fill

Every mutation happens inside `lock`, because the web dashboard reads these
structures from its own threads while the trading loop writes them.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_today() -> str:
    return _utc_now().strftime("%Y-%m-%d")


@dataclass
class Position:
    """One open lot of the base token bought by the bot."""

    id: str
    opened_at: str
    base_amount: int  # base token smallest units
    quote_spent: int  # quote token smallest units
    entry_price: float  # quote UI units per base UI unit
    signature: str | None  # None when opened in dry run

    @property
    def cost_basis_ui(self) -> float:
        return self.quote_spent

    def unrealized_pnl_quote_units(
        self, current_price: float, base_decimals: int, quote_decimals: int
    ) -> float:
        """Unrealized PnL in quote UI units at `current_price`.

        `current_price` and the result are in quote UI units per base UI
        unit. `base_decimals` and `quote_decimals` are required because this
        object stores amounts in each token's smallest unit -- mixing the
        two scales silently is how money math goes wrong.
        """
        value_ui = (self.base_amount / (10 ** base_decimals)) * current_price
        cost_ui = self.quote_spent / (10 ** quote_decimals)
        return value_ui - cost_ui


@dataclass
class BotState:
    positions: list[Position] = field(default_factory=list)
    daily_date: str = field(default_factory=_utc_today)
    daily_quote_spent: int = 0

    def roll_daily_window(self) -> None:
        """Zero the spend counter when the UTC day changes."""
        today = _utc_today()
        if self.daily_date != today:
            log.info(
                "New UTC day (%s -> %s); resetting daily spend counter.",
                self.daily_date, today,
            )
            self.daily_date = today
            self.daily_quote_spent = 0


class StateStore:
    """Loads, saves, and mutates the bot's persisted state."""

    def __init__(self, working_dir: Path):
        self.state_path = working_dir / "state.json"
        self.trades_path = working_dir / "trades.jsonl"
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> BotState:
        if not self.state_path.exists():
            return BotState()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"{self.state_path} exists but could not be parsed: {exc}. "
                f"Fix or delete it before starting; refusing to overwrite it."
            ) from exc

        positions = [Position(**p) for p in raw.get("positions", [])]
        return BotState(
            positions=positions,
            daily_date=raw.get("daily_date", _utc_today()),
            daily_quote_spent=int(raw.get("daily_quote_spent", 0)),
        )

    def save(self) -> None:
        with self.lock:
            payload = {
                "positions": [asdict(p) for p in self.state.positions],
                "daily_date": self.state.daily_date,
                "daily_quote_spent": self.state.daily_quote_spent,
            }
            # Write-then-rename so a crash mid-write cannot corrupt the file.
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)

    # ── reads (safe from other threads) ──────────────────────────────────

    def positions_snapshot(self) -> list[Position]:
        """Deep-enough copy of open positions for concurrent readers."""
        with self.lock:
            return [replace(p) for p in self.state.positions]

    def daily_spend(self) -> tuple[str, int]:
        with self.lock:
            return self.state.daily_date, self.state.daily_quote_spent

    # ── mutations ───────────────────────────────────────────────────────

    def open_position(
        self,
        base_amount: int,
        quote_spent: int,
        entry_price: float,
        signature: str | None,
    ) -> Position:
        with self.lock:
            self.state.roll_daily_window()
            position = Position(
                id=f"{_utc_now().strftime('%Y%m%dT%H%M%S')}-{len(self.state.positions)}",
                opened_at=_utc_now().isoformat(),
                base_amount=base_amount,
                quote_spent=quote_spent,
                entry_price=entry_price,
                signature=signature,
            )
            self.state.positions.append(position)
            self.state.daily_quote_spent += quote_spent
            self.save()
        return position

    def close_position(self, position_id: str) -> Position | None:
        with self.lock:
            for i, position in enumerate(self.state.positions):
                if position.id == position_id:
                    removed = self.state.positions.pop(i)
                    self.save()
                    return removed
        log.warning("Tried to close unknown position %s", position_id)
        return None

    def remove_position(self, position_id: str) -> None:
        """Drop a position from the book without treating it as a fill."""
        with self.lock:
            self.state.positions = [
                p for p in self.state.positions if p.id != position_id
            ]
            self.save()

    def log_trade(self, record: dict) -> None:
        """Append a line to the audit log. Never raises into the trade loop."""
        record = {**record, "logged_at": _utc_now().isoformat()}
        with self.lock:
            try:
                with self.trades_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            except OSError as exc:
                log.error("Could not write trade log: %s", exc)

    def read_trades(self, limit: int = 500) -> list[dict]:
        """Newest-first tail of the audit log, for the dashboard."""
        with self.lock:
            if not self.trades_path.exists():
                return []
            try:
                lines = self.trades_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                log.error("Could not read trade log: %s", exc)
                return []

        out: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written trailing line is not worth failing over
            if len(out) >= limit:
                break
        return out
