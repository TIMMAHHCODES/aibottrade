#!/usr/bin/env python3
"""Withdraw from the bot's wallet back to an address you control.

    # preview -- shows the amounts and does nothing else
    python withdraw.py --to <ADDRESS> --amount 10 --token USDC

    # actually move the funds
    python withdraw.py --to <ADDRESS> --amount 10 --token USDC --send

    # everything the wallet holds
    python withdraw.py --to <ADDRESS> --amount all --token SOL --send

Without `--send` nothing is signed and nothing is broadcast, so you always
see the exact numbers first.

The private key is read straight out of `.env`, signs in this process, and
is never printed, never written to a second file, and never sent anywhere.
This is the intended way to get funds back out. Importing the key into
Phantom or Solflare would work too, but it would put the key on your screen
and inside that application's security model -- and it would mean the
bot's wallet and your main wallet share one secret.
"""

from __future__ import annotations

import argparse
import base64
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import base58
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from bot.config import load_config
from bot.wallet import Wallet, WalletError

# Program IDs. These are the same on every Solana network.
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")

LAMPORTS_PER_SOL = 1_000_000_000

# A transaction pays its fee whether or not it succeeds, and the fee comes
# out of the sending wallet. `--amount all` leaves this much behind so the
# wallet is not stranded with nothing to pay for its next transaction.
SOL_FEE_BUFFER_LAMPORTS = 10_000  # 0.00001 SOL

# Roughly one signature plus a little headroom, used to refuse a token
# withdrawal that could not possibly pay its own fee.
SOL_MIN_FOR_TOKEN_FEE_LAMPORTS = 20_000

# Short names, so the command is hard to mistype. A raw mint address still
# works for anything else.
KNOWN_TOKENS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "WSOL": "So11111111111111111111111111111111111111112",
}


# ── formatting ─────────────────────────────────────────────────────────


def fmt(units: int, decimals: int) -> str:
    """Smallest units -> a readable string. No floats anywhere."""
    value = Decimal(units) / (Decimal(10) ** decimals)
    text = f"{value:.{min(decimals, 9)}f}".rstrip("0").rstrip(".")
    return text or "0"


def to_units(value: Decimal, decimals: int) -> int:
    """A decimal amount -> integer smallest units, without float rounding."""
    scaled = value * (Decimal(10) ** decimals)
    units = int(scaled)
    if units != scaled:
        print(f"  note: {value} carries more than {decimals} decimals; using {units}")
    return units


# ── instructions ───────────────────────────────────────────────────────


def associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Where `owner`'s account for `mint` lives, by convention."""
    address, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )
    return address


def transfer_checked_instruction(
    source: Pubkey,
    mint: Pubkey,
    destination: Pubkey,
    owner: Pubkey,
    amount: int,
    decimals: int,
) -> Instruction:
    """SPL `TransferChecked` (instruction 12).

    Plain `Transfer` (3) would also move the tokens, but it does not verify
    the mint or the decimal count, so a mistyped `--token` could silently
    move the wrong amount of the wrong asset. The checked variant refuses.
    """
    return Instruction(
        TOKEN_PROGRAM,
        bytes([12]) + int(amount).to_bytes(8, "little") + bytes([int(decimals)]),
        [
            AccountMeta(source, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(destination, is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=True, is_writable=False),
        ],
    )


def create_ata_instruction(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    """Create `owner`'s account for `mint`.

    A transfer to an address that has never held that token fails on chain
    unless the account exists first, so this rides along in the same
    transaction when needed. The sender pays its rent, once.
    """
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        bytes([0]),
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(
                associated_token_address(owner, mint), is_signer=False, is_writable=True
            ),
            AccountMeta(owner, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ],
    )


def parse_destination(text: str) -> Pubkey | None:
    """Validate a base58 address and turn it into a Pubkey, or None."""
    try:
        raw = base58.b58decode(text.strip())
    except Exception:  # base58 raises several unrelated types
        return None
    if len(raw) != 32:
        return None
    try:
        return Pubkey.from_bytes(raw)
    except Exception:
        return None


def parse_amount(text: str) -> Decimal | None:
    """`all`/`max` -> None meaning the whole balance; otherwise a Decimal.

    Raises SystemExit on anything that is not a positive number. It has to
    raise rather than return None: None already means "send everything", so
    returning it for an error would turn a typo into a full withdrawal.
    """
    if text.strip().lower() in {"all", "max"}:
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise SystemExit(f"That is not a number: {text!r}")
    if value <= 0:
        raise SystemExit("Amount must be greater than zero.")
    return value


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="withdraw.py",
        description="Move funds out of the bot's wallet to an address you control.",
        epilog="Without --send this only previews: nothing is signed, nothing is sent.",
    )
    parser.add_argument("--to", required=True, help="destination address (base58)")
    parser.add_argument(
        "--token", default="SOL",
        help="SOL (default), USDC, WSOL, or a full mint address",
    )
    parser.add_argument("--amount", required=True, help="how much to send, or 'all'")
    parser.add_argument(
        "--send", action="store_true",
        help="actually broadcast the transfer (default: preview only)",
    )
    args = parser.parse_args()

    destination = parse_destination(args.to)
    if destination is None:
        print(f"That does not look like a Solana address: {args.to}")
        print("It should be about 44 characters of base58, e.g. Hkd49...ckSE")
        return 1

    working_dir = Path(__file__).resolve().parent
    try:
        cfg = load_config(working_dir)
        wallet = Wallet(cfg.private_key_base58, cfg.rpc_url)
    except (WalletError, Exception) as exc:  # noqa: BLE001 - report, do not traceback
        print(f"Could not load the wallet: {exc}")
        return 1

    try:
        if destination == wallet.pubkey:
            print("That is this wallet's own address. Nothing would move.")
            return 1

        # ── work out what we are sending ───────────────────────────────
        spec = args.token.strip().upper()
        mint: Pubkey | None = None
        label = "SOL"
        decimals = 9
        source_ata: Pubkey | None = None

        if spec != "SOL":
            mint_str = KNOWN_TOKENS.get(spec, spec)
            try:
                mint = Pubkey.from_string(mint_str)
            except Exception:
                print(f"Unknown token {args.token!r}.")
                print("Use SOL, USDC, WSOL, or a full mint address.")
                return 1
            label = spec if spec in KNOWN_TOKENS else f"{str(mint)[:8]}..."

            source_ata = wallet.token_account_address(str(mint))
            if source_ata is None:
                print(f"This wallet holds no {label}. Nothing to withdraw.")
                return 1
            info = wallet.get_token_account_balance(str(source_ata))
            decimals = int(info["decimals"])

        sol_lamports = wallet.get_sol_lamports()
        balance_units = sol_lamports if mint is None else int(info["amount"])

        requested = parse_amount(args.amount)
        if requested is None:
            units = (
                balance_units - SOL_FEE_BUFFER_LAMPORTS
                if mint is None
                else balance_units
            )
        else:
            units = to_units(requested, decimals)

        if units <= 0:
            print(f"Not enough to send: the wallet holds {fmt(balance_units, decimals)} {label}.")
            return 1
        if units > balance_units:
            print(
                f"That is more than the balance "
                f"({fmt(balance_units, decimals)} {label})."
            )
            return 1

        # A token transfer still pays its fee in SOL. Catching this here is
        # worth much more than discovering it after a failed broadcast.
        if mint is not None and sol_lamports < SOL_MIN_FOR_TOKEN_FEE_LAMPORTS:
            print(f"The wallet holds {fmt(sol_lamports, 9)} SOL -- not enough for the network fee.")
            print("Send about 0.01 SOL to this wallet first, then retry.")
            return 1

        # ── show exactly what will happen ──────────────────────────────
        print()
        print("From   :", wallet.pubkey)
        print("To     :", destination)
        print("Token  :", label if mint is None else f"{label} ({mint})")
        print("Balance:", fmt(balance_units, decimals), label)
        print("Sending:", fmt(units, decimals), label)
        print("Fee    :", "paid from the amount (SOL)" if mint is None else "paid in SOL")
        if mint is None and requested is None:
            print(f"Keeps  : {fmt(SOL_FEE_BUFFER_LAMPORTS, 9)} SOL for future fees")

        if not args.send:
            print()
            print("Preview only. Nothing was signed and nothing was sent.")
            print("To go ahead, add --send:")
            print(
                f"  python withdraw.py --to {args.to} "
                f"--amount {args.amount} --token {args.token} --send"
            )
            return 0

        # ── build, sign, send ──────────────────────────────────────────
        print()
        print("Building transaction...")
        blockhash = wallet.get_recent_blockhash()

        instructions: list[Instruction] = []
        if mint is None:
            instructions.append(
                transfer(
                    TransferParams(
                        from_pubkey=wallet.pubkey, to_pubkey=destination, lamports=units
                    )
                )
            )
        else:
            destination_ata = associated_token_address(destination, mint)
            if not wallet.account_exists(str(destination_ata)):
                instructions.append(
                    create_ata_instruction(wallet.pubkey, destination, mint)
                )
                print("  the receiving address has never held this token;")
                print("  creating its account in the same transaction")
            instructions.append(
                transfer_checked_instruction(
                    source_ata, mint, destination_ata, wallet.pubkey, units, decimals
                )
            )

        message = Message.new_with_blockhash(instructions, wallet.pubkey, blockhash)
        transaction = VersionedTransaction(message, [wallet.keypair])

        signature = wallet.send_raw(base64.b64encode(bytes(transaction)).decode())
        print("  broadcast, signature:", signature)
        print("  https://solscan.io/tx/" + signature)

        print("  waiting for confirmation", end="", flush=True)
        confirmed = False
        for _ in range(30):
            status = wallet.get_signature_status(signature)
            if status is not None and status.get("confirmationStatus") in (
                "confirmed", "finalized",
            ):
                print()
                if status.get("err"):
                    print("  FAILED on chain:", status["err"])
                    return 1
                print("  confirmed.")
                confirmed = True
                break
            print(".", end="", flush=True)
            time.sleep(2)
        if not confirmed:
            print()
            print("  Not confirmed yet. Check the link above before retrying --")
            print("  an unconfirmed transfer may still land.")
            return 1

        after = (
            wallet.get_sol_lamports()
            if mint is None
            else wallet.get_token_balance(str(mint))
        )
        print()
        print(f"  This wallet now holds {fmt(after, decimals)} {label}.")
        return 0
    finally:
        wallet.close()


if __name__ == "__main__":
    raise SystemExit(main())
