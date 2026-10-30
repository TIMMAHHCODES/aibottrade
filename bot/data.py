"""Market data.

Jupiter's swap API is for execution, not for charting, so candles come from
GeckoTerminal's free public API (no key required, ~30 req/min). If you later
want a paid feed (Birdeye, Helius DAS, Shyft), only this module needs to change.
"""

from __future__ import annotations

import logging
import time

import httpx

from .config import KNOWN_DECIMALS, UNIT_MAP, Config

log = logging.getLogger(__name__)

# GeckoTerminal asks that clients identify themselves; it also buys headroom
# against rate limiting.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "solana-jupiter-bot/1.0",
}


class DataError(Exception):
    """Raised when market data cannot be fetched or parsed."""


# Trending pools below this 24h volume are noise, not discovery. Tuned to
# keep the list readable rather than to be exhaustive.
MIN_TRENDING_VOLUME_USD = 50_000.0


def _as_float(value) -> float | None:
    """GeckoTerminal sends numbers as strings, and null for missing ones."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketData:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._http = httpx.Client(headers=_HEADERS, timeout=20.0)
        self._pool_address: str | None = None
        self._resolved_at: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MarketData":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── internals ───────────────────────────────────────────────────────

    _MAX_ATTEMPTS = 5

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                response = self._http.get(url, params=params)

                if response.status_code == 429:
                    # Rate limited. GeckoTerminal's free tier allows roughly
                    # 30 requests/minute, so honour Retry-After when the
                    # server sends it and otherwise back off exponentially.
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else 2.0 ** attempt
                    except ValueError:
                        wait = 2.0 ** attempt
                    wait = min(wait, 60.0)
                    log.warning(
                        "Rate limited by GeckoTerminal; sleeping %.1fs (attempt %d/%d)",
                        wait, attempt, self._MAX_ATTEMPTS,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as exc:
                if attempt == self._MAX_ATTEMPTS:
                    raise DataError(
                        f"HTTP {exc.response.status_code} from {url}: "
                        f"{exc.response.text[:200]}"
                    ) from exc
                time.sleep(1.0 * attempt)
            except httpx.HTTPError as exc:
                if attempt == self._MAX_ATTEMPTS:
                    raise DataError(f"Network error fetching {url}: {exc}") from exc
                time.sleep(1.0 * attempt)

        raise DataError(f"Exhausted retries fetching {url}")

    # ── public API ──────────────────────────────────────────────────────

    def token_decimals(self, mint: str) -> int:
        """Look up a token's decimals, falling back to the known table."""
        if mint in KNOWN_DECIMALS:
            return KNOWN_DECIMALS[mint]
        try:
            payload = self._get_json(
                f"{self.cfg.geckoterminal_base_url}/networks/solana/tokens/{mint}"
            )
            return int(payload["data"]["attributes"]["decimals"])
        except (DataError, KeyError, TypeError, ValueError) as exc:
            raise DataError(f"Could not resolve decimals for token {mint}: {exc}") from exc

    def resolve_pool(self) -> str:
        """Find the deepest Solana pool pairing BASE with QUOTE.

        Cached for an hour so the hot loop does not re-resolve every candle.
        """
        if self._pool_address and (time.time() - self._resolved_at) < 3600:
            return self._pool_address

        payload = self._get_json(
            f"{self.cfg.geckoterminal_base_url}/networks/solana/tokens/"
            f"{self.cfg.base_mint}/pools",
            params={"sort": "h24_volume_usd_desc", "page": 1},
        )

        wanted = {self.cfg.base_mint, self.cfg.quote_mint}
        candidates = []
        for pool in payload.get("data", []):
            rel = pool.get("relationships", {})
            token_ids = {
                rel.get("base_token", {}).get("data", {}).get("id", ""),
                rel.get("quote_token", {}).get("data", {}).get("id", ""),
            }
            mints = {t.split("_", 1)[-1] for t in token_ids if t}
            if mints == wanted:
                candidates.append(pool)

        if not candidates:
            raise DataError(
                f"No Solana pool found pairing "
                f"{self.cfg.base_symbol} and {self.cfg.quote_symbol}. "
                f"The quote mint may not trade against this base token."
            )

        # GeckoTerminal returns pools ordered by 24h volume, so the first
        # candidate is already the most liquid.
        self._pool_address = candidates[0]["id"].split("_", 1)[-1]
        self._resolved_at = time.time()
        log.info("Using pool %s for %s/%s",
                 self._pool_address, self.cfg.base_symbol, self.cfg.quote_symbol)
        return self._pool_address

    def fetch_closes(self, limit: int = 250) -> list[float]:
        """Return up to `limit` closing prices, oldest first, newest last.

        The most recent GeckoTerminal candle is the *in-progress* one, so it
        is dropped. Trading off a still-forming candle is the single most
        common source of backtest-vs-live divergence.
        """
        pool = self.resolve_pool()
        unit, aggregate = UNIT_MAP[self.cfg.timeframe_minutes]
        payload = self._get_json(
            f"{self.cfg.geckoterminal_base_url}/networks/solana/pools/"
            f"{pool}/ohlcv/{unit}",
            params={
                "aggregate": aggregate,
                "limit": min(limit + 5, 1000),
                "currency": "usd",
            },
        )

        try:
            rows = payload["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError) as exc:
            raise DataError(f"Unexpected OHLCV payload shape: {payload}") from exc

        # GeckoTerminal returns [timestamp, open, high, low, close, volume],
        # newest first. Drop the in-progress candle, then reverse to oldest-first.
        rows = sorted(rows, key=lambda r: r[0])
        if len(rows) < 2:
            raise DataError("Not enough candles returned by GeckoTerminal.")

        closes = [float(row[4]) for row in rows[:-1]]
        log.debug("Fetched %d closed candles, latest close=%.6g", len(closes), closes[-1])
        return closes

    def fetch_price(self) -> float:
        """Current mid price of BASE in QUOTE terms."""
        pool = self.resolve_pool()
        payload = self._get_json(
            f"{self.cfg.geckoterminal_base_url}/networks/solana/pools/{pool}"
        )
        try:
            return float(payload["data"]["attributes"]["base_token_price_usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(f"Could not read price from pool payload: {exc}") from exc

    def fetch_ohlcv(self, limit: int = 300) -> list[dict]:
        """Full OHLCV candles for the dashboard chart, oldest first.

        Same source and the same in-progress-candle drop as `fetch_closes`,
        but keeps high/low/volume so the chart has something to draw. The
        trading loop still uses `fetch_closes`: keeping the two separate
        means a charting change cannot silently alter what gets traded.
        """
        pool = self.resolve_pool()
        unit, aggregate = UNIT_MAP[self.cfg.timeframe_minutes]
        payload = self._get_json(
            f"{self.cfg.geckoterminal_base_url}/networks/solana/pools/"
            f"{pool}/ohlcv/{unit}",
            params={
                "aggregate": aggregate,
                "limit": min(limit + 5, 1000),
                "currency": "usd",
            },
        )

        try:
            rows = payload["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError) as exc:
            raise DataError(f"Unexpected OHLCV payload shape: {exc}") from exc

        rows = sorted(rows, key=lambda r: r[0])
        if len(rows) < 2:
            raise DataError("Not enough candles returned by GeckoTerminal.")

        # Drop the forming candle, exactly as fetch_closes does.
        out = []
        for row in rows[:-1]:
            try:
                out.append({
                    "time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except (TypeError, ValueError, IndexError):
                continue  # a malformed row is not worth failing the chart over
        return out[-limit:]

    def fetch_trending(self, limit: int = 30) -> list[dict]:
        """Trending Solana pools, filtered to ones with real volume.

        Read-only discovery data. There is deliberately no path from here to
        a swap: "this token is pumping" is not a trading signal, and handing
        a beginner a buy button next to a meme chart is how accounts get
        emptied. Configure a pair explicitly, in `.env`, if you want it.
        """
        payload = self._get_json(
            f"{self.cfg.geckoterminal_base_url}/networks/solana/trending_pools",
            params={"page": 1},
        )

        out: list[dict] = []
        for pool in payload.get("data", []):
            attrs = pool.get("attributes", {}) or {}
            rel = pool.get("relationships", {}) or {}

            base_id = (rel.get("base_token", {}).get("data", {}) or {}).get("id", "")
            base_mint = base_id.split("_", 1)[-1] if base_id else ""

            name = attrs.get("name") or ""
            parts = [p.strip() for p in name.split("/")]

            price = _as_float(attrs.get("base_token_price_usd"))
            volume = _as_float((attrs.get("volume_usd") or {}).get("h24"))
            market_cap = _as_float(attrs.get("market_cap_usd"))

            # A token with no 24h volume is either broken or brand new; a
            # token with no USD price has no market at all. Neither belongs
            # in a list someone might read as "worth looking at".
            if price is None or volume is None or volume < MIN_TRENDING_VOLUME_USD:
                continue

            out.append({
                "pool": pool.get("id", ""),
                "name": name,
                "baseSymbol": parts[0] if parts else "?",
                "quoteSymbol": parts[1] if len(parts) > 1 else "?",
                "baseMint": base_mint,
                "priceUsd": price,
                "volume24h": volume,
                "marketCap": market_cap,
            })
            if len(out) >= limit:
                break
        return out
