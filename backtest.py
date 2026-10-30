#!/usr/bin/env python3
"""Backtest the bot's strategy on real historical candles.

    python backtest.py                       # 180 days, SOL/USDC 15m
    python backtest.py --days 90
    python backtest.py --cost-bps 40         # one cost level only

This answers one question honestly: would this strategy have made money on
data it has never seen? It trades nothing and signs nothing.

Three decisions worth knowing about, all made pessimistically on purpose:

  * Entries fill at the NEXT candle's open, not at the signal candle's close.
    You cannot act on a candle that has not finished forming.

  * When a candle spans both stop-loss and take-profit, the stop-loss is
    assumed to have hit first. Real fills are ambiguous; assume the worse one.

  * Costs are charged on entry AND exit. There is no free round trip.

It also prints buy-and-hold for the same period, because beating a strategy
is easy if the market fell 30% while you were flat.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.config import load_config          # noqa: E402
from bot.data import MarketData             # noqa: E402
from bot.strategy import ema, evaluate, rsi  # noqa: E402
from bot.strategy import Signal             # noqa: E402

HEADERS = {"Accept": "application/json", "User-Agent": "solana-jupiter-bot/1.0"}

# Floor on the wait after a 429. GeckoTerminal's free tier allows roughly 30
# calls a minute, and the running bot draws from the same quota, so being
# polite here costs a minute and saves a failed run.
MIN_WAIT_SECONDS = 8.0


# ── data ────────────────────────────────────────────────────────────────

def fetch_history(days: int, cfg, sleep_s: float = 4.0) -> list[dict]:
    """Page backwards through GeckoTerminal OHLCV, oldest first.

    One request returns at most 1000 candles, so a year of 15-minute bars
    needs ~35 requests. GeckoTerminal's free tier allows about 30 per
    minute, hence the sleep.
    """
    md = MarketData(cfg)
    pool = md.resolve_pool()
    md.close()

    per_day = (24 * 60) // cfg.timeframe_minutes
    wanted = days * per_day
    url = f"{cfg.geckoterminal_base_url}/networks/solana/pools/{pool}/ohlcv/minute"

    rows: list[list] = []
    before: int | None = None
    with httpx.Client(headers=HEADERS, timeout=40.0) as client:
        while len(rows) < wanted:
            params = {"aggregate": str(cfg.timeframe_minutes), "limit": 1000,
                      "currency": "usd"}
            if before is not None:
                params["before_timestamp"] = before

            payload = None
            for attempt in range(1, 9):
                response = client.get(url, params=params)
                if response.status_code == 429:
                    # GeckoTerminal sends `Retry-After: 0`, which is useless on
                    # its own, so treat it as a hint and always wait at least
                    # MIN_WAIT_SECONDS. The running bot shares this quota.
                    try:
                        wait = float(response.headers.get("Retry-After") or 0.0)
                    except ValueError:
                        wait = 0.0
                    wait = max(wait, MIN_WAIT_SECONDS * attempt)
                    print(f"  rate limited, waiting {wait:.0f}s "
                          f"(attempt {attempt}/8)", flush=True)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                payload = response.json()
                break
            if payload is None:
                raise SystemExit(
                    "GeckoTerminal kept rate limiting. Stop the bot and retry, "
                    "or come back in a few minutes -- both share one free quota."
                )

            batch = (payload.get("data", {}).get("attributes", {}) or {}).get("ohlcv_list") or []
            if not batch:
                break
            rows.extend(batch)
            before = int(min(r[0] for r in batch))
            print(f"  {len(rows):>7,} candles", end="\r", flush=True)
            time.sleep(sleep_s)

    print()
    # Newest-first API output, duplicates across pages, and the in-progress
    # candle all have to go before this is usable.
    seen: dict[int, list] = {}
    for row in rows:
        seen[int(row[0])] = row
    closed = sorted(seen.values(), key=lambda r: r[0])[:-1]

    return [
        {"time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
         "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
        for r in closed
    ]


# ── signals, computed once over the whole series ───────────────────────

def signal_series(closes: list[float], cfg) -> list[str]:
    """BUY / SELL / HOLD for every candle, aligned to `closes`.

    The live bot calls `evaluate()` on a growing window each cycle; doing
    that here would be O(n^2) and far too slow across tens of thousands of
    candles. This computes each indicator once and walks the aligned
    series, which produces the same answers -- `verify_against_evaluate`
    checks exactly that.
    """
    fast = ema(closes, cfg.ema_fast)
    slow = ema(closes, cfg.ema_slow)
    relative = rsi(closes, cfg.rsi_period)
    if not fast or not slow or not relative:
        raise SystemExit("Not enough history to warm up the indicators.")

    fast_start = len(closes) - len(fast)
    slow_start = len(closes) - len(slow)
    rsi_start = len(closes) - len(relative)

    first = max(fast_start, slow_start, rsi_start) + 1
    out = ["HOLD"] * len(closes)

    for i in range(first, len(closes)):
        f, s = i - fast_start, i - slow_start
        crossed_up = fast[f - 1] <= slow[s - 1] and fast[f] > slow[s]
        crossed_down = fast[f - 1] >= slow[s - 1] and fast[f] < slow[s]
        if crossed_up:
            r = relative[i - rsi_start]
            out[i] = "BUY" if cfg.rsi_min_entry <= r <= cfg.rsi_max_entry else "HOLD"
        elif crossed_down:
            out[i] = "SELL"
    return out


def verify_against_evaluate(closes: list[float], cfg, signals: list[str]) -> int:
    """Spot-check the fast path against the bot's real `evaluate()`.

    Returns how many sampled candles disagreed. If this is not zero the
    backtest is measuring something the live bot would never do.
    """
    step = max(1, len(closes) // 400)
    checked = mismatches = 0
    for i in range(cfg.ema_slow + cfg.rsi_period, len(closes), step):
        expected, _ = evaluate(closes[: i + 1], cfg)
        if expected.value != signals[i]:
            mismatches += 1
        checked += 1
    print(f"  verified {checked} sampled candles against bot.strategy.evaluate: "
          f"{mismatches} disagreements")
    return mismatches


# ── simulation ──────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    reason: str

    @property
    def pnl(self) -> float:
        return self.exit - self.entry


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    max_drawdown: float = 0.0

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def net(self) -> float:
        return sum(t.pnl for t in self.trades)


def simulate(candles: list[dict], signals: list[str], cfg, cost: float) -> Result:
    """Walk the candles once, holding at most one position.

    `cost` is the one-way cost as a fraction (0.002 = 20 bps), charged by
    paying more on entry and receiving less on exit.
    """
    size = cfg.trade_size_quote
    tp_pct = cfg.take_profit_pct
    sl_pct = cfg.stop_loss_pct

    result = Result()
    entry: float | None = None
    entry_index = 0
    equity = 0.0
    peak = 0.0

    for i, candle in enumerate(candles):
        # Fill an entry ordered on the previous candle, at this open.
        if entry is None and signals[i] == "BUY" and i + 1 < len(candles):
            entry = candles[i + 1]["open"] * (1.0 + cost)
            entry_index = i + 1
            continue

        if entry is None:
            continue

        units = size / entry
        take_profit = entry * (1.0 + tp_pct)
        stop_loss = entry * (1.0 - sl_pct)

        exit_price = reason = None
        # Both levels inside one candle is genuinely ambiguous. Assume the
        # stop-loss fired: the pessimistic choice, stated openly.
        if candle["low"] <= stop_loss:
            exit_price, reason = stop_loss * (1.0 - cost), "SL"
        elif candle["high"] >= take_profit:
            exit_price, reason = take_profit * (1.0 - cost), "TP"
        elif signals[i] == "SELL" and i > entry_index:
            exit_price, reason = candle["close"] * (1.0 - cost), "SIGNAL"
        elif i == len(candles) - 1:
            exit_price, reason = candle["close"] * (1.0 - cost), "OPEN"

        if exit_price is None:
            continue

        result.trades.append(Trade(
            entry_time=candles[entry_index]["time"],
            exit_time=candle["time"],
            entry=entry,
            exit=exit_price,
            reason=reason,
        ))
        equity += units * (exit_price - entry)
        peak = max(peak, equity)
        result.max_drawdown = max(result.max_drawdown, peak - equity)
        entry = None

    return result


# ── report ──────────────────────────────────────────────────────────────

def report(candles, signals, cfg, bankroll: float) -> None:
    closes = [c["close"] for c in candles]
    first_day = time.strftime("%Y-%m-%d", time.gmtime(candles[0]["time"]))
    last_day = time.strftime("%Y-%m-%d", time.gmtime(candles[-1]["time"]))

    buys = signals.count("BUY")
    sells = signals.count("SELL")
    hold_change = (closes[-1] / closes[0] - 1.0) * 100.0

    print()
    print(f"Period   : {first_day} .. {last_day}  ({len(candles):,} closed 15m candles)")
    print(f"SOL/USDC : {closes[0]:.2f} -> {closes[-1]:.2f}   ({hold_change:+.1f}%)")
    print(f"Signals  : {buys} BUY, {sells} SELL   (EMA {cfg.ema_fast}/{cfg.ema_slow}, "
          f"RSI {cfg.rsi_period}, filter {cfg.rsi_min_entry:.0f}-{cfg.rsi_max_entry:.0f})")
    print(f"Position : {cfg.trade_size_quote:.2f} USDC fixed, TP {cfg.take_profit_pct * 100:.0f}%, "
          f"SL {cfg.stop_loss_pct * 100:.0f}%")
    print(f"Bankroll : {bankroll:.0f} USDC (for the % column)")
    print()
    print("  cost       trades  wins   win%    net USDC   vs bankroll   maxDD")
    print("  ---------  ------  -----  -----  ---------  -----------  ------")

    for bps in (0, 20, 50, 100):
        result = simulate(candles, signals, cfg, bps / 10_000.0)
        n = len(result.trades)
        if n == 0:
            print(f"  {bps:>3} bps        0      -      -       +0.00         0.0%      -")
            continue
        win_pct = 100.0 * result.wins / n
        pct = 100.0 * result.net / bankroll
        dd = 100.0 * result.max_drawdown / bankroll
        print(f"  {bps:>3} bps      {n:>4}  {result.wins:>5}  {win_pct:>4.1f}%"
              f"  {result.net:>+9.2f}   {pct:>+9.1f}%   {dd:>5.1f}%")

    print()
    print("  (0 bps = perfect execution, no spread. 100 bps = what SLIPPAGE_BPS is set to.)")


def resample(candles: list[dict], factor: int) -> list[dict]:
    """Fold `factor` candles into one, so 15m data can be tested as 1h/4h.

    Ranges accumulate (the group's high is its highest high) while open and
    close come from the ends. Using close-only aggregation here would invent
    candles that never existed and make the stop-loss checks meaningless.
    """
    out: list[dict] = []
    for i in range(0, len(candles) - factor + 1, factor):
        group = candles[i: i + factor]
        out.append({
            "time": group[0]["time"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "volume": sum(c["volume"] for c in group),
        })
    return out


@dataclass
class ScanRow:
    frame: str
    label: str
    trades: int
    wins: int
    net: float
    max_dd: float
    hold_pct: float


def scan(candles15: list[dict], cfg, cost_bps: float, bankroll: float) -> list[ScanRow]:
    """Try several timeframe and indicator combinations on the SAME data.

    Every row is tested against one shared history, so the comparison is
    fair. Nothing here is validated out of sample: treat the best row as a
    hypothesis to check on fresh data, never as a result.
    """
    combos = [
        ("EMA 5/13", 5, 13, 14),
        ("EMA 9/21 (yours)", 9, 21, 14),
        ("EMA 12/26", 12, 26, 14),
        ("EMA 20/50", 20, 50, 14),
        ("EMA 12/26 RSI21", 12, 26, 21),
    ]
    frames = [("15m", 1), ("1h", 4), ("4h", 16)]

    rows: list[ScanRow] = []
    for frame_name, factor in frames:
        candles = candles15 if factor == 1 else resample(candles15, factor)
        closes = [c["close"] for c in candles]
        hold_pct = (closes[-1] / closes[0] - 1.0) * 100.0

        for label, fast, slow, period in combos:
            if len(candles) < slow + period + 50:
                continue
            trial = replace(cfg, ema_fast=fast, ema_slow=slow, rsi_period=period)
            try:
                signals = signal_series(closes, trial)
            except SystemExit:
                continue
            result = simulate(candles, signals, trial, cost_bps / 10_000.0)
            rows.append(ScanRow(
                frame=frame_name,
                label=label,
                trades=len(result.trades),
                wins=result.wins,
                net=result.net,
                max_dd=result.max_drawdown,
                hold_pct=hold_pct,
            ))
    return rows


def report_scan(rows: list[ScanRow], cost_bps: int, bankroll: float) -> None:
    rows = sorted(rows, key=lambda r: r.net, reverse=True)
    print()
    print(f"Variant scan at {cost_bps} bps round-trip cost, "
          f"{bankroll:.0f} USDC bankroll")
    print()
    print("  frame  strategy          trades  wins   win%     net USDC   vs bankroll")
    print("  -----  ----------------  ------  -----  -----  -----------  -----------")
    for row in rows:
        win_pct = (100.0 * row.wins / row.trades) if row.trades else 0.0
        pct = 100.0 * row.net / bankroll
        marker = "  <-- you" if "yours" in row.label else ""
        print(f"  {row.frame:>5}  {row.label:<17}  {row.trades:>6}  "
              f"{row.wins:>5}  {win_pct:>4.1f}%  {row.net:>+11.2f}  {pct:>+10.1f}%"
              f"{marker}")

    best = rows[0] if rows else None
    if best is not None:
        print()
        print(f"  Buy and hold over the same period: {best.hold_pct:+.1f}%")
        print()
        print("  IN-SAMPLE WARNING: every row above was measured on the same")
        print("  data the settings were chosen from. The best row is a guess,")
        print("  not a result. Before believing it, run it on a period the")
        print("  settings never saw.")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="backtest.py",
        description="Replay the strategy over historical SOL/USDC candles.",
    )
    parser.add_argument("--days", type=int, default=180, help="how far back (default 180)")
    parser.add_argument("--bankroll", type=float, default=25.0,
                        help="capital the %% column is measured against (default 25)")
    parser.add_argument("--cost-bps", type=int, default=None,
                        help="print trades for one cost level in detail")
    parser.add_argument("--scan", action="store_true",
                        help="test several timeframes and EMA/RSI combinations")
    args = parser.parse_args()

    cfg = load_config(Path(__file__).resolve().parent)
    if cfg.timeframe_minutes != 15:
        print(f"NOTE: your config uses {cfg.timeframe_minutes}m candles.")

    print(f"Fetching {args.days} days of {cfg.timeframe_minutes}m candles...")
    candles = fetch_history(args.days, cfg)
    if len(candles) < cfg.ema_slow + cfg.rsi_period + 50:
        print(f"Only {len(candles)} candles returned -- too few to test.")
        return 1

    closes = [c["close"] for c in candles]
    print("Computing signals...")
    signals = signal_series(closes, cfg)
    if verify_against_evaluate(closes, cfg, signals):
        print("WARNING: the fast path disagrees with bot.strategy.evaluate.")
        print("         Treat these results as unreliable.")

    if args.scan:
        report_scan(scan(candles, cfg, 20.0, args.bankroll), 20, args.bankroll)
        return 0

    report(candles, signals, cfg, args.bankroll)

    if args.cost_bps is not None:
        result = simulate(candles, signals, cfg, args.cost_bps / 10_000.0)
        print()
        print(f"  Every trade at {args.cost_bps} bps (entry HH:MM UTC):")
        for trade in result.trades[:60]:
            stamp = time.strftime("%m-%d %H:%M", time.gmtime(trade.entry_time))
            print(f"    {stamp}  {trade.entry:8.2f} -> {trade.exit:8.2f}  "
                  f"{trade.pnl:+.3f} USDC  [{trade.reason}]")
        if len(result.trades) > 60:
            print(f"    ... and {len(result.trades) - 60} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
