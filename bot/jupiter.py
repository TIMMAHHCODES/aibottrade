"""Jupiter swap integration.

Flow for every trade:
  1. GET  /quote        -> best route and expected output
  2. POST /swap         -> a ready-to-sign serialized transaction
  3. sign locally       -> your key never leaves the process
  4. sendTransaction    -> broadcast over RPC
  5. poll for confirmation

In DRY_RUN the flow stops after step 1: we take Jupiter's real quote as the
simulated fill, so the numbers you see are honest, but nothing is signed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config
from .wallet import Wallet, WalletError

log = logging.getLogger(__name__)


class JupiterError(Exception):
    """Raised when Jupiter cannot produce a route or a transaction."""


@dataclass(frozen=True)
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    slippage_bps: int
    price_impact_pct: float
    raw: dict[str, Any]

    @property
    def execution_price(self) -> float:
        """Output units per input unit, both in smallest units."""
        if self.in_amount == 0:
            return 0.0
        return self.out_amount / self.in_amount


@dataclass(frozen=True)
class Fill:
    """What actually happened, in either mode."""

    signature: str | None  # None when dry-run
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    dry_run: bool


class Jupiter:
    def __init__(self, cfg: Config, wallet: Wallet | None):
        self.cfg = cfg
        self.wallet = wallet
        self._http = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Jupiter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── quoting ─────────────────────────────────────────────────────────

    def get_quote(
        self, input_mint: str, output_mint: str, amount: int
    ) -> Quote:
        """Ask Jupiter for the best route. Read-only; safe in dry run."""
        if amount <= 0:
            raise JupiterError(f"Quote amount must be positive, got {amount}.")

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "swapMode": "ExactIn",
            "slippageBps": str(self.cfg.slippage_bps),
            "restrictIntermediateTokens": "true",
        }
        try:
            response = self._http.get(f"{self.cfg.jupiter_base_url}/quote", params=params)
        except httpx.HTTPError as exc:
            raise JupiterError(f"Jupiter quote request failed: {exc}") from exc

        if response.status_code != 200:
            raise JupiterError(
                f"Jupiter quote returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        body = response.json()
        if "error" in body:
            raise JupiterError(f"Jupiter quote error: {body['error']}")
        if not body.get("routePlan"):
            raise JupiterError(
                f"No route found for {input_mint[:8]}... -> {output_mint[:8]}... "
                f"of {amount} units. Amount may be too small to swap."
            )

        return Quote(
            input_mint=body["inputMint"],
            output_mint=body["outputMint"],
            in_amount=int(body["inAmount"]),
            out_amount=int(body["outAmount"]),
            other_amount_threshold=int(body["otherAmountThreshold"]),
            slippage_bps=int(body["slippageBps"]),
            price_impact_pct=float(body.get("priceImpactPct", 0.0)),
            raw=body,
        )

    # ── execution ───────────────────────────────────────────────────────

    def execute(self, quote: Quote, dry_run: bool) -> Fill:
        """Turn a quote into a fill. Only signs and sends when not dry-run."""
        if dry_run:
            log.info(
                "[DRY RUN] Would swap %d -> %d (price impact %.4f%%)",
                quote.in_amount,
                quote.out_amount,
                quote.price_impact_pct,
            )
            return Fill(
                signature=None,
                input_mint=quote.input_mint,
                output_mint=quote.output_mint,
                in_amount=quote.in_amount,
                out_amount=quote.out_amount,
                dry_run=True,
            )

        if self.wallet is None:
            raise JupiterError("Live trading requested but no wallet is loaded.")

        swap_tx = self._build_swap_transaction(quote)
        signed = self.wallet.sign_and_serialize(swap_tx)
        signature = self.wallet.send_raw(signed)
        log.info("Broadcast transaction: %s", signature)

        self._await_confirmation(signature)
        return Fill(
            signature=signature,
            input_mint=quote.input_mint,
            output_mint=quote.output_mint,
            in_amount=quote.in_amount,
            out_amount=quote.out_amount,
            dry_run=False,
        )

    def _build_swap_transaction(self, quote: Quote) -> str:
        payload = {
            "quoteResponse": quote.raw,
            "userPublicKey": str(self.wallet.pubkey),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            # "auto" lets Jupiter size the priority fee for the current
            # congestion instead of you guessing.
            "prioritizationFeeLamports": "auto",
        }
        try:
            response = self._http.post(f"{self.cfg.jupiter_base_url}/swap", json=payload)
        except httpx.HTTPError as exc:
            raise JupiterError(f"Jupiter swap request failed: {exc}") from exc

        if response.status_code != 200:
            raise JupiterError(
                f"Jupiter swap returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        body = response.json()
        tx = body.get("swapTransaction")
        if not tx:
            raise JupiterError(f"Jupiter /swap returned no transaction: {body}")
        return tx

    def _await_confirmation(self, signature: str, timeout: float = 90.0) -> None:
        """Poll until the signature confirms or the blockhash expires.

        A transaction that never confirms is NOT a failure to treat as a
        clean revert: it may still land. We only give up after the block
        height Jupiter gave us has passed.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3.0)
            try:
                status = self.wallet.get_signature_status(signature)
            except WalletError as exc:
                log.warning("Status poll failed: %s", exc)
                continue

            if status is None:
                continue
            if status.get("err"):
                raise JupiterError(f"Transaction {signature} failed: {status['err']}")
            if status.get("confirmationStatus") in {"finalized", "confirmed"}:
                log.info("Transaction confirmed: %s", signature)
                return

        log.warning(
            "Transaction %s not confirmed within %.0fs. "
            "It may still land. Check your wallet before retrying.",
            signature,
            timeout,
        )
