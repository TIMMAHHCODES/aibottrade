"""Run every test suite.

Usage:  python run_tests.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SUITES = [
    "test_strategy.py",
    "test_risk.py",
    "test_engine.py",
    "test_web.py",
    "test_withdraw.py",
]


def run(suite: str) -> int:
    print()
    print("=" * 70)
    print(f"  {suite}")
    print("=" * 70)

    # Import and invoke each suite in-process so a failure cannot be
    # hidden by a later suite, and so all results are reported together.
    module_name = suite[:-3]
    started = time.time()
    try:
        module = __import__(module_name)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not import {suite}: {type(exc).__name__}: {exc}")
        return 1

    # Redirect the module's own prints through unchanged; it reports
    # pass/fail per test and returns 0/1 from main().
    return int(module.main())


def main() -> int:
    results: dict[str, int] = {}
    for suite in SUITES:
        results[suite] = run(suite)

    print()
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    failed = 0
    for suite, code in results.items():
        status = "PASS" if code == 0 else f"FAIL (exit {code})"
        failed += 0 if code == 0 else 1
        print(f"  {suite:<22} {status}")

    print()
    if failed:
        print(f"{failed} of {len(results)} suites FAILED.")
        return 1
    print(f"All {len(results)} suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
