"""Wallet handling.

The private key is loaded from the environment as a base58 string. It is
never written to disk by this bot, never logged, and never leaves the process.
"""

from __future__ import annotations

import base64
import logging

import base58
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey

log = logging.getLogger(__name__)


class WalletError(Exception):
    """Raised when the wallet cannot be loaded or queried."""


class Wallet:
    def __init__(self, private_key_base58: str, rpc_url: str):
        try:
            raw = base58.b58decode(private_key_base58.strip())
        except Exception as exc:  # base58 raises several types
            raise WalletError(
                "PRIVATE_KEY_BASE58 is not valid base58. "
                "Export it from Phantom: Settings -> Export Private Key."
            ) from exc

        # Phantom-style exports are 64 bytes (32 secret + 32 public).
        # On-chain generated keys are sometimes 32 bytes of secret only.
        if len(raw) == 64:
            self.keypair = Keypair.from_bytes(raw)
        elif len(raw) == 32:
            self.keypair = Keypair.from_seed(raw)
        else:
            raise WalletError(
                f"PRIVATE_KEY_BASE58 decoded to {len(raw)} bytes; expected 32 or 64."
            )

        self.pubkey: Pubkey = self.keypair.pubkey()
        self._http = httpx.Client(timeout=25.0)
        self._rpc_url = rpc_url
        log.info("Wallet loaded: %s", self.pubkey)

    def close(self) -> None:
        self._http.close()

    # ── RPC helpers ─────────────────────────────────────────────────────

    def _rpc(self, method: str, params: list) -> dict:
        try:
            response = self._http.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise WalletError(f"RPC {method} network failure: {exc}") from exc

        if "error" in body:
            raise WalletError(f"RPC {method} error: {body['error']}")
        return body["result"]

    def get_sol_lamports(self) -> int:
        """Native SOL balance, used to confirm the wallet can pay fees."""
        result = self._rpc("getBalance", [str(self.pubkey)])
        return int(result["value"])

    def get_recent_blockhash(self):
        """Recent blockhash, needed to build a transaction locally."""
        from solders.hash import Hash

        result = self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        return Hash.from_string(result["value"]["blockhash"])

    def account_exists(self, address: str) -> bool:
        """Whether an account is on chain (has lamports or is initialized)."""
        result = self._rpc(
            "getAccountInfo",
            [address, {"encoding": "base64", "commitment": "confirmed"}],
        )
        return result.get("value") is not None

    def get_token_account_balance(self, address: str) -> dict:
        """Smallest-unit amount and decimals for one specific token account."""
        result = self._rpc(
            "getTokenAccountBalance",
            [address, {"commitment": "confirmed"}],
        )
        return result["value"]

    def token_account_address(self, mint: str):
        """Address of this wallet's largest token account holding `mint`.

        Returns None when the wallet holds none -- the normal state before
        it has ever received that token.

        Deliberately not used to implement `get_token_balance`: that one runs
        on the live trading path and is a single round trip. Making it call
        this would add a second RPC request to every risk check for the sake
        of removing a few duplicated lines.
        """
        from solders.pubkey import Pubkey

        result = self._rpc(
            "getTokenAccountsByOwner",
            [
                str(self.pubkey),
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
        best: tuple[int, Pubkey] | None = None
        for account in result.get("value", []):
            try:
                info = account["account"]["data"]["parsed"]["info"]
                if info.get("state", "initialized") != "initialized":
                    continue
                amount = int(info["tokenAmount"]["amount"])
                address = Pubkey.from_string(account["pubkey"])
            except (KeyError, TypeError, ValueError):
                continue
            if best is None or amount > best[0]:
                best = (amount, address)
        return best[1] if best else None

    def get_token_balance(self, mint: str) -> int:
        """Largest SPL token balance for `mint` (smallest units).

        Returns 0 when the account does not exist yet, which is the normal
        state for a freshly funded wallet.
        """
        ata_owner = str(self.pubkey)
        result = self._rpc(
            "getTokenAccountsByOwner",
            [
                ata_owner,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
        best = 0
        for account in result.get("value", []):
            try:
                info = account["account"]["data"]["parsed"]["info"]
                amount = int(info["tokenAmount"]["amount"])
                # Ignore frozen and non-transferable accounts.
                if info.get("state", "initialized") != "initialized":
                    continue
                best = max(best, amount)
            except (KeyError, TypeError, ValueError):
                continue
        return best

    def get_signature_status(self, signature: str) -> dict | None:
        """Status for a signature, or None if the RPC has not seen it yet."""
        result = self._rpc("getSignatureStatuses", [[signature]])
        values = result.get("value", [])
        return values[0] if values else None

    def get_block_height(self) -> int:
        result = self._rpc("getBlockHeight", [{"commitment": "confirmed"}])
        return int(result)

    def sign_and_serialize(self, tx_b64: str) -> str:
        """Sign a base64 serialized transaction and return it re-encoded."""
        # Imported here so that DRY_RUN never needs solders' transaction code.
        from solders.transaction import VersionedTransaction

        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        except Exception as exc:
            raise WalletError(f"Jupiter returned an undecodable transaction: {exc}") from exc

        tx.sign([self.keypair])
        return base64.b64encode(bytes(tx)).decode()

    def send_raw(self, tx_b64: str) -> str:
        """Broadcast a signed transaction and return its signature."""
        result = self._rpc(
            "sendTransaction",
            [
                tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                },
            ],
        )
        return str(result)
