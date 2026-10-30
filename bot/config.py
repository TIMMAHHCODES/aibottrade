"""Configuration loading and validation.

All tunables come from a `.env` file, overridable by real environment
variables. Nothing here touches the network.

Note the use of `dotenv_values` rather than `load_dotenv`: the bot can reload
its config while running (driven by the web dashboard's settings editor).
`load_dotenv` writes into `os.environ` and, by default, refuses to overwrite
an existing key -- so the first load would win forever and every reload
silently be a no-op. `dotenv_values` reads the file without mutating global
state, which makes reload actually work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# Solana mainnet mints the bot knows decimals for, so we can skip a metadata
# round-trip. Anything unknown is looked up at runtime.
KNOWN_DECIMALS: dict[str, int] = {
    "So11111111111111111111111111111111111111112": 9,  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,  # USDT
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": 6,  # BONK
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": 6,  # JUP
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": 6,  # HNT
}

# GeckoTerminal's OHLCV endpoint takes a *unit* in the path and a multiplier
# as a query param, and it only accepts specific aggregates per unit. These
# were confirmed against the live API, which reports its allowed values in
# the 400 response body. Do not add combinations without checking: an
# unsupported aggregate returns HTTP 400, not an empty array.
#
#   minute: 1, 5, 15      hour: 1, 4, 12      day: 1
UNIT_MAP: dict[int, tuple[str, int]] = {
    1: ("minute", 1),
    5: ("minute", 5),
    15: ("minute", 15),
    60: ("hour", 1),
    240: ("hour", 4),
    720: ("hour", 12),
    1440: ("day", 1),
}

VALID_TIMEFRAMES = set(UNIT_MAP)
assert set(UNIT_MAP) == VALID_TIMEFRAMES, "UNIT_MAP and VALID_TIMEFRAMES disagree"


class ConfigError(Exception):
    """Raised when configuration is missing or internally inconsistent."""


class _Reader:
    """Resolves a config key against shell env, then `.env`, then default.

    Precedence is shell > file > default, which lets you override a running
    bot from the command line without editing `.env`.
    """

    def __init__(self, file_values: dict[str, str | None]):
        self.file_values = file_values

    def get(self, name: str, default: str | None = None) -> str | None:
        shell = os.environ.get(name)
        if shell is not None and shell != "":
            return shell
        value = self.file_values.get(name)
        if value is not None and value != "":
            return value
        return default

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise ConfigError(
                f"Missing required config value: {name}. "
                f"Copy .env.example to .env and fill it in."
            )
        return value

    def as_float(self, name: str, default: str) -> float:
        raw = self.require_or(name, default)
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc

    def as_int(self, name: str, default: str) -> int:
        raw = self.require_or(name, default)
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc

    def as_bool(self, name: str, default: str) -> bool:
        raw = self.require_or(name, default).strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")

    def require_or(self, name: str, default: str) -> str:
        value = self.get(name, default)
        if value is None:
            raise ConfigError(
                f"Missing required config value: {name}. "
                f"Copy .env.example to .env and fill it in."
            )
        return value


@dataclass(frozen=True)
class Config:
    # Safety
    dry_run: bool

    # Wallet
    private_key_base58: str

    # Pair
    base_mint: str
    base_symbol: str
    quote_mint: str
    quote_symbol: str

    # Strategy
    timeframe_minutes: int
    ema_fast: int
    ema_slow: int
    rsi_period: int
    rsi_max_entry: float
    rsi_min_entry: float

    # Risk
    trade_size_quote: float
    max_quote_per_day: float
    max_open_positions: int
    slippage_bps: int
    take_profit_pct: float
    stop_loss_pct: float
    quote_reserve: float
    cooldown_seconds: int

    # Dashboard
    ui_host: str
    ui_port: int
    ui_enabled: bool

    # Infrastructure
    rpc_url: str
    jupiter_base_url: str
    geckoterminal_base_url: str
    kill_switch_file: Path

    # Derived
    base_decimals: int
    quote_decimals: int
    working_dir: Path

    # ── derived helpers ──────────────────────────────────────────────────

    @property
    def trade_size_base_units(self) -> int:
        """Trade size expressed in the quote token's smallest unit."""
        return int(round(self.trade_size_quote * (10 ** self.quote_decimals)))

    @property
    def ui_url(self) -> str:
        return f"http://{self.ui_host}:{self.ui_port}"

    def quote_to_ui(self, amount: int) -> float:
        """Smallest units -> human readable quote amount."""
        return amount / (10 ** self.quote_decimals)

    def base_to_ui(self, amount: int) -> float:
        """Smallest units -> human readable base amount."""
        return amount / (10 ** self.base_decimals)


def validate(cfg: Config) -> None:
    """Cross-field checks shared by startup and by the settings editor.

    Called on every load so that a bad value saved from the UI is rejected
    before it replaces a working configuration.
    """
    if cfg.ema_fast >= cfg.ema_slow:
        raise ConfigError(
            f"EMA_FAST ({cfg.ema_fast}) must be strictly less than "
            f"EMA_SLOW ({cfg.ema_slow})."
        )
    if cfg.timeframe_minutes not in VALID_TIMEFRAMES:
        raise ConfigError(
            f"TIMEFRAME_MINUTES must be one of {sorted(VALID_TIMEFRAMES)}, "
            f"got {cfg.timeframe_minutes}."
        )
    if not 1 <= cfg.slippage_bps <= 5000:
        raise ConfigError(f"SLIPPAGE_BPS must be between 1 and 5000, got {cfg.slippage_bps}.")
    if cfg.trade_size_quote <= 0:
        raise ConfigError(f"TRADE_SIZE_QUOTE must be positive, got {cfg.trade_size_quote}.")
    if cfg.trade_size_quote > cfg.max_quote_per_day:
        raise ConfigError(
            f"TRADE_SIZE_QUOTE ({cfg.trade_size_quote}) cannot exceed "
            f"MAX_QUOTE_PER_DAY ({cfg.max_quote_per_day})."
        )
    if cfg.base_mint == cfg.quote_mint:
        raise ConfigError("BASE_MINT and QUOTE_MINT must be different tokens.")
    if cfg.max_open_positions < 1:
        raise ConfigError(
            f"MAX_OPEN_POSITIONS must be at least 1, got {cfg.max_open_positions}."
        )
    if cfg.quote_reserve < 0:
        raise ConfigError(f"QUOTE_RESERVE cannot be negative, got {cfg.quote_reserve}.")
    if cfg.rsi_max_entry <= cfg.rsi_min_entry:
        raise ConfigError(
            f"RSI_MAX_ENTRY ({cfg.rsi_max_entry}) must be greater than "
            f"RSI_MIN_ENTRY ({cfg.rsi_min_entry})."
        )
    if not cfg.dry_run and not cfg.private_key_base58:
        raise ConfigError(
            "DRY_RUN=false but PRIVATE_KEY_BASE58 is empty. "
            "Set a key, or set DRY_RUN=true."
        )
    if cfg.take_profit_pct <= 0 or cfg.stop_loss_pct <= 0:
        raise ConfigError(
            "TAKE_PROFIT_PCT and STOP_LOSS_PCT must both be positive "
            f"(got {cfg.take_profit_pct} and {cfg.stop_loss_pct})."
        )
    if not 1 <= cfg.ui_port <= 65535:
        raise ConfigError(f"UI_PORT must be 1-65535, got {cfg.ui_port}.")


def load_config(base_dir: Path | None = None) -> Config:
    """Load and validate config from `.env` in `base_dir`.

    Safe to call repeatedly; it re-reads the file every time so the running
    bot can pick up edits made from the dashboard.
    """
    base_dir = (base_dir or Path.cwd()).resolve()
    env = _Reader(dotenv_values(base_dir / ".env"))

    base_mint = env.require("BASE_MINT")
    quote_mint = env.require("QUOTE_MINT")
    ui_host = env.require_or("UI_HOST", "127.0.0.1").strip()

    cfg = Config(
        dry_run=env.as_bool("DRY_RUN", "true"),
        private_key_base58=(env.get("PRIVATE_KEY_BASE58") or "").strip(),
        base_mint=base_mint,
        base_symbol=env.require_or("BASE_SYMBOL", "BASE"),
        quote_mint=quote_mint,
        quote_symbol=env.require_or("QUOTE_SYMBOL", "QUOTE"),
        timeframe_minutes=env.as_int("TIMEFRAME_MINUTES", "15"),
        ema_fast=env.as_int("EMA_FAST", "9"),
        ema_slow=env.as_int("EMA_SLOW", "21"),
        rsi_period=env.as_int("RSI_PERIOD", "14"),
        rsi_max_entry=env.as_float("RSI_MAX_ENTRY", "65"),
        rsi_min_entry=env.as_float("RSI_MIN_ENTRY", "35"),
        trade_size_quote=env.as_float("TRADE_SIZE_QUOTE", "5.0"),
        max_quote_per_day=env.as_float("MAX_QUOTE_PER_DAY", "20.0"),
        max_open_positions=env.as_int("MAX_OPEN_POSITIONS", "2"),
        slippage_bps=env.as_int("SLIPPAGE_BPS", "100"),
        take_profit_pct=env.as_float("TAKE_PROFIT_PCT", "0.04"),
        stop_loss_pct=env.as_float("STOP_LOSS_PCT", "0.03"),
        quote_reserve=env.as_float("QUOTE_RESERVE", "0.05"),
        cooldown_seconds=env.as_int("COOLDOWN_SECONDS", "120"),
        ui_host=ui_host,
        ui_port=env.as_int("UI_PORT", "8787"),
        ui_enabled=env.as_bool("UI_ENABLED", "true"),
        rpc_url=env.require_or("RPC_URL", "https://api.mainnet-beta.solana.com"),
        jupiter_base_url=env.require_or(
            "JUPITER_BASE_URL", "https://lite-api.jup.ag/swap/v1"
        ).rstrip("/"),
        geckoterminal_base_url=env.require_or(
            "GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com/api/v2"
        ).rstrip("/"),
        kill_switch_file=base_dir / env.require_or("KILL_SWITCH_FILE", "kill.txt"),
        base_decimals=KNOWN_DECIMALS.get(base_mint, 9),
        quote_decimals=KNOWN_DECIMALS.get(quote_mint, 6),
        working_dir=base_dir,
    )

    if cfg.rsi_period < 2:
        raise ConfigError(f"RSI_PERIOD must be at least 2, got {cfg.rsi_period}.")
    if ui_host not in {"127.0.0.1", "localhost", "::1"}:
        # Not fatal -- some people do want LAN access -- but it must be a
        # deliberate, visible choice, since the dashboard can stop trades.
        import logging

        logging.getLogger(__name__).warning(
            "UI_HOST=%s exposes the dashboard beyond this machine. Anyone "
            "who can reach it can stop and start trading. Use 127.0.0.1 "
            "unless you know you need otherwise.",
            ui_host,
        )

    validate(cfg)
    return cfg
