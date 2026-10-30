#!/usr/bin/env python3
"""Solana / Jupiter trading bot - entry point.

Usage:
  python main.py check    # verify config + connectivity, no trading
  python main.py once     # one decision cycle, then exit
  python main.py run      # trade forever on the candle close
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bot.config import ConfigError, load_config
from bot.engine import Engine
from bot.web import DashboardServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Simple EMA/RSI trading bot for Solana, executing via Jupiter.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "once", "check"],
        help="run = loop forever, once = single cycle, check = diagnostics only.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default INFO).",
    )
    return parser


def setup_logging(level: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    # Everything also goes to a file, which is what you will actually read
    # when something goes wrong at 3am.
    logfile = logging.FileHandler(log_dir / "bot.log", encoding="utf-8")
    logfile.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(logfile)

    # httpx logs every request at INFO, which drowns out the bot's own
    # decision trail. Keep it available at DEBUG when you need it.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    args = build_parser().parse_args()

    try:
        cfg = load_config(Path(__file__).resolve().parent)
    except ConfigError as exc:
        print(f"\nCONFIG ERROR: {exc}\n", file=sys.stderr)
        print("Copy .env.example to .env and fill in the values.\n", file=sys.stderr)
        return 2

    setup_logging(args.log_level, cfg.working_dir)

    engine = Engine(cfg)
    dashboard: DashboardServer | None = None
    try:
        if args.command == "check":
            return 0 if engine.check_connectivity() else 1

        if args.command == "once":
            status = engine.run_once()
            logging.getLogger(__name__).info("Cycle result: %s", status)
            return 0

        # The dashboard only makes sense for a bot that is actually looping.
        if cfg.ui_enabled:
            dashboard = DashboardServer(engine)
            if not dashboard.start():
                # A busy port is worth complaining about but not worth
                # stopping the bot over -- the kill switch still works from
                # the filesystem, and you can fix the port and restart.
                dashboard = None

        engine.run_forever()
        return 0
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by user.")
        return 0
    finally:
        if dashboard is not None:
            dashboard.stop()
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
