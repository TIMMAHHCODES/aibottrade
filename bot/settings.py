"""The settings the dashboard is allowed to edit, and how to apply them.

Anything the UI can change lives here. Keys not in this table are rejected
outright, so a hand-crafted request cannot smuggle arbitrary entries into
`.env`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VALID_TIMEFRAMES, Config, ConfigError, load_config
from .envfile import update_env


class SettingError(Exception):
    """Raised when a requested settings change is invalid or rejected."""


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str  # "int" | "float" | "choice" | "bool"
    group: str
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple = ()
    unit: str = ""
    placeholder: str = ""

    @property
    def read_only(self) -> bool:
        return self.kind == "readonly"


def _tf_choices() -> tuple:
    labels = {1: "1 min", 5: "5 min", 15: "15 min", 60: "1 hour",
              240: "4 hours", 720: "12 hours", 1440: "1 day"}
    return tuple(
        {"value": v, "label": labels.get(v, f"{v} min")}
        for v in sorted(VALID_TIMEFRAMES)
    )


SETTINGS: tuple[Setting, ...] = (
    # ── strategy ─────────────────────────────────────────────────────────
    Setting(
        key="TIMEFRAME_MINUTES", label="Candle timeframe", kind="choice",
        group="Strategy", choices=_tf_choices(),
        help="Only these values exist on GeckoTerminal; others return HTTP 400.",
    ),
    Setting(
        key="EMA_FAST", label="Fast EMA", kind="int", group="Strategy",
        minimum=2, maximum=100,
        help="Must stay below the slow EMA.",
    ),
    Setting(
        key="EMA_SLOW", label="Slow EMA", kind="int", group="Strategy",
        minimum=3, maximum=400,
        help="Larger = fewer, smoother signals.",
    ),
    Setting(
        key="RSI_PERIOD", label="RSI period", kind="int", group="Strategy",
        minimum=2, maximum=100,
    ),
    Setting(
        key="RSI_MAX_ENTRY", label="Max RSI to buy", kind="float",
        group="Strategy", minimum=0, maximum=100, step=1,
        help="Skip the entry when RSI is at or above this.",
    ),
    Setting(
        key="RSI_MIN_ENTRY", label="Min RSI to buy", kind="float",
        group="Strategy", minimum=0, maximum=100, step=1,
        help="Falling-knife guard. Dormant at EMA 9/21; see README.",
    ),

    # ── risk ─────────────────────────────────────────────────────────────
    Setting(
        key="TRADE_SIZE_QUOTE", label="Size per trade", kind="float",
        group="Risk", minimum=0.000001, step=0.5,
        unit="quote", help="In quote units (USDC). Applied to every entry.",
    ),
    Setting(
        key="MAX_QUOTE_PER_DAY", label="Daily cap", kind="float",
        group="Risk", minimum=0.000001, step=1.0,
        unit="quote", help="Hard ceiling on total spend per UTC day.",
    ),
    Setting(
        key="MAX_OPEN_POSITIONS", label="Max open positions", kind="int",
        group="Risk", minimum=1, maximum=100,
    ),
    Setting(
        key="SLIPPAGE_BPS", label="Max slippage", kind="int", group="Risk",
        minimum=1, maximum=5000, step=10, unit="bps",
        help="100 bps = 1%. Routes with worse impact are refused, not filled.",
    ),
    Setting(
        key="TAKE_PROFIT_PCT", label="Take profit", kind="float", group="Risk",
        minimum=0.0001, maximum=10, step=0.005,
        help="As a fraction: 0.04 = +4%.",
    ),
    Setting(
        key="STOP_LOSS_PCT", label="Stop loss", kind="float", group="Risk",
        minimum=0.0001, maximum=10, step=0.005,
        help="As a fraction: 0.03 = -3%. Your real downside limit.",
    ),
    Setting(
        key="QUOTE_RESERVE", label="Quote reserve", kind="float", group="Risk",
        minimum=0, step=0.01, unit="quote",
        help="Left untouched so network fees can always be paid.",
    ),
    Setting(
        key="COOLDOWN_SECONDS", label="Cooldown", kind="int", group="Risk",
        minimum=0, maximum=86400, step=30, unit="sec",
        help="Minimum gap between fills.",
    ),

    # ── shown but locked ─────────────────────────────────────────────────
    Setting(
        key="BASE_MINT", label="Base mint", kind="readonly", group="Pair",
        help="Locked. Changing the pair while holding positions would "
             "orphan them. Edit .env and restart to change this.",
    ),
    Setting(
        key="QUOTE_MINT", label="Quote mint", kind="readonly", group="Pair",
        help="Locked for the same reason.",
    ),
    Setting(
        key="DRY_RUN", label="Mode", kind="readonly", group="Pair",
        help="Change via the mode switch, which requires confirmation.",
    ),
)


SETTING_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}
EDITABLE_KEYS: frozenset[str] = frozenset(
    s.key for s in SETTINGS if not s.read_only
)

# Resolved Config attribute name for each editable key, so the dashboard
# displays what is actually running rather than what the file happens to say.
_ATTR_BY_KEY: dict[str, str] = {
    "TIMEFRAME_MINUTES": "timeframe_minutes",
    "EMA_FAST": "ema_fast",
    "EMA_SLOW": "ema_slow",
    "RSI_PERIOD": "rsi_period",
    "RSI_MAX_ENTRY": "rsi_max_entry",
    "RSI_MIN_ENTRY": "rsi_min_entry",
    "TRADE_SIZE_QUOTE": "trade_size_quote",
    "MAX_QUOTE_PER_DAY": "max_quote_per_day",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "SLIPPAGE_BPS": "slippage_bps",
    "TAKE_PROFIT_PCT": "take_profit_pct",
    "STOP_LOSS_PCT": "stop_loss_pct",
    "QUOTE_RESERVE": "quote_reserve",
    "COOLDOWN_SECONDS": "cooldown_seconds",
    "BASE_MINT": "base_mint",
    "QUOTE_MINT": "quote_mint",
    "DRY_RUN": "dry_run",
}


def current_values(cfg: Config) -> dict[str, str]:
    """Live value of every exposed setting, as a string for the form."""
    values: dict[str, str] = {}
    for setting in SETTINGS:
        attr = _ATTR_BY_KEY.get(setting.key)
        if attr is None:
            continue
        raw = getattr(cfg, attr)
        values[setting.key] = "true" if raw is True else "false" if raw is False else str(raw)
    return values


def schema() -> list[dict]:
    """Serializable form description for the browser."""
    out = []
    for s in SETTINGS:
        out.append({
            "key": s.key,
            "label": s.label,
            "kind": s.kind,
            "group": s.group,
            "help": s.help,
            "min": s.minimum,
            "max": s.maximum,
            "step": s.step,
            "choices": list(s.choices),
            "unit": s.unit,
            "readOnly": s.read_only,
        })
    return out


def apply_changes(cfg: Config, changes: dict[str, str]) -> Config:
    """Persist `changes` to `.env`, validate, and roll back on failure.

    Validation reuses the real `load_config`, so a value accepted here is
    accepted at startup too -- there is no second, weaker ruleset. If
    anything fails the original file is restored byte for byte, leaving the
    running bot on its known-good configuration.
    """
    if not changes:
        return cfg

    unknown = [k for k in changes if k not in EDITABLE_KEYS]
    if unknown:
        raise SettingError(
            f"Refusing to set {', '.join(sorted(unknown))}: not an editable "
            f"setting. Allowed: {', '.join(sorted(EDITABLE_KEYS))}."
        )

    return _persist_and_validate(cfg, changes)


def _persist_and_validate(cfg: Config, changes: dict[str, str]) -> Config:
    """Write to `.env`, validate, and restore the original on any failure.

    This is the shared mechanism behind both the settings form and the mode
    switch. It deliberately enforces no key policy of its own -- that lives
    in `apply_changes` (which keys the form may touch) and `set_dry_run`
    (which requires a key before going live).
    """
    path = cfg.working_dir / ".env"
    original = path.read_text(encoding="utf-8") if path.exists() else None

    cleaned = {k: str(v).strip() for k, v in changes.items()}
    if any(v == "" for v in cleaned.values()):
        raise SettingError("Settings cannot be empty. Leave a field untouched "
                           "instead of clearing it.")

    try:
        update_env(path, cleaned)
        new_cfg = load_config(cfg.working_dir)
    except (ConfigError, OSError) as exc:
        _restore(path, original)
        raise SettingError(str(exc)) from exc

    return new_cfg


def _restore(path: Path, original: str | None) -> None:
    try:
        if original is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(original, encoding="utf-8")
    except OSError:
        # Nothing better to do; load_config's error is the one worth showing.
        pass


def set_dry_run(cfg: Config, dry_run: bool) -> Config:
    """Switch between dry run and live trading.

    Going live requires a key to already be present -- this function will
    not accept one being supplied here, because pasting a seed phrase into
    a browser form is a habit that eventually loses the seed phrase. Set it
    in `.env` first, then flip the switch.
    """
    if dry_run == cfg.dry_run:
        return cfg
    if not dry_run and not cfg.private_key_base58:
        raise SettingError(
            "Cannot go live: PRIVATE_KEY_BASE58 is empty in .env. "
            "Add the key there first, then flip the switch."
        )
    # DRY_RUN is excluded from EDITABLE_KEYS so the settings form cannot
    # touch it; the mode switch goes around that gate on purpose.
    return _persist_and_validate(cfg, {"DRY_RUN": "true" if dry_run else "false"})
