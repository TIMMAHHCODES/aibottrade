"""Self-test for withdraw.py's pure logic. No network access, no key needed.

Run:  python test_withdraw.py

These cover the parts that decide *how much* and *where to*: the arithmetic
that converts human amounts into on-chain units, and the exact bytes of the
instructions. Getting either wrong moves the wrong amount of the wrong
asset, so they are checked byte by byte rather than by eye.

The broadcast path is deliberately not tested here. It needs a funded
wallet, and a test that could spend real money is worse than no test -- the
first real withdrawal is always done as a small one by hand.
"""

from __future__ import annotations

import contextlib
import io
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from solders.keypair import Keypair  # noqa: E402

import withdraw as W  # noqa: E402


def new_pubkey():
    return Keypair().pubkey()


# ── amount and display arithmetic ───────────────────────────────────────


def test_fmt_trims_trailing_zeros() -> None:
    assert W.fmt(1_500_000, 6) == "1.5", W.fmt(1_500_000, 6)
    assert W.fmt(0, 6) == "0", W.fmt(0, 6)
    assert W.fmt(1_000_000_000, 9) == "1", W.fmt(1_000_000_000, 9)
    assert W.fmt(123, 6) == "0.000123", W.fmt(123, 6)
    print("ok  fmt trims trailing zeros and handles zero")


def test_to_units_has_no_float_drift() -> None:
    """The classic 0.1 + 0.2 trap must not eat or mint a cent."""
    assert W.to_units(Decimal("1"), 6) == 1_000_000
    assert W.to_units(Decimal("0.1"), 6) == 100_000
    # 19.99 at 6 decimals is exactly 19990000; float arithmetic gives
    # 19989999.999999997 and would truncate to the wrong number.
    assert W.to_units(Decimal("19.99"), 6) == 19_990_000
    assert W.to_units(Decimal("0.05"), 9) == 50_000_000
    print("ok  to_units converts without float drift")


def test_to_units_truncates_excess_precision() -> None:
    """A sub-unit remainder is dropped, and the caller is told about it."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        units = W.to_units(Decimal("1.0000009"), 6)
    assert units == 1_000_000, units
    assert "note" in captured.getvalue(), captured.getvalue()
    print("ok  to_units warns about precision it has to drop")


def test_parse_amount_accepts_all_and_max() -> None:
    """None means the whole balance, and nothing else does."""
    assert W.parse_amount("all") is None
    assert W.parse_amount("MAX") is None
    assert W.parse_amount("  All  ") is None
    print("ok  parse_amount recognises all and max")


def test_parse_amount_accepts_numbers() -> None:
    assert W.parse_amount("10") == Decimal("10")
    assert W.parse_amount("0.05") == Decimal("0.05")
    assert W.parse_amount(" 7.25 ") == Decimal("7.25")
    print("ok  parse_amount accepts plain numbers")


def test_parse_amount_refuses_junk() -> None:
    """A typo must abort. Returning None here would mean 'send everything'."""
    for bad in ("abc", "1.2.3", "", "10 USDC", "--", "1e"):
        try:
            W.parse_amount(bad)
        except SystemExit:
            continue
        raise AssertionError(f"expected SystemExit for {bad!r}, got a value")

    # A negative amount is nonsense, not a transfer in the other direction.
    for bad in ("-5", "0"):
        try:
            W.parse_amount(bad)
        except SystemExit:
            continue
        raise AssertionError(f"expected SystemExit for {bad!r}, got a value")
    print("ok  parse_amount refuses junk, zero and negatives")


# ── destination validation ──────────────────────────────────────────────


def test_parse_destination_accepts_a_real_address() -> None:
    key = new_pubkey()
    assert W.parse_destination(str(key)) == key
    # Surrounding whitespace is common when pasting; it must not matter.
    assert W.parse_destination(f"  {key}\n") == key
    print("ok  parse_destination accepts a well-formed address")


def test_parse_destination_refuses_junk() -> None:
    for bad in (
        "",
        "not-an-address",
        "nxPqZ4F7ux8Vph5",           # too short
        "0" * 44,                    # right length, wrong alphabet
        str(new_pubkey()) + "AAAA",  # too long
    ):
        assert W.parse_destination(bad) is None, bad
    print("ok  parse_destination refuses junk, short and long input")


def test_parse_destination_refuses_the_wrong_length() -> None:
    """base58 can decode to the right alphabet but the wrong byte count."""
    # 31 bytes of 0x00 is not a valid public key.
    assert W.parse_destination(W.base58.b58encode(b"\x00" * 31).decode()) is None
    assert W.parse_destination(W.base58.b58encode(b"\x00" * 33).decode()) is None
    print("ok  parse_destination enforces the 32-byte length")


# ── instruction layout ──────────────────────────────────────────────────


def test_transfer_checked_layout() -> None:
    source, mint, dest, owner = (new_pubkey() for _ in range(4))
    ix = W.transfer_checked_instruction(source, mint, dest, owner, 1_234_567, 6)

    assert ix.program_id == W.TOKEN_PROGRAM, ix.program_id
    assert len(ix.data) == 10, len(ix.data)
    # 12 is TransferChecked; the rest is u64 LE amount then the decimal count.
    assert ix.data[0] == 12, ix.data[0]
    assert ix.data[1:9] == (1_234_567).to_bytes(8, "little"), ix.data[1:9]
    assert ix.data[9] == 6, ix.data[9]

    metas = ix.accounts
    assert len(metas) == 4, len(metas)
    assert metas[0].pubkey == source and metas[0].is_writable and not metas[0].is_signer
    assert metas[1].pubkey == mint and not metas[1].is_writable
    assert metas[2].pubkey == dest and metas[2].is_writable
    assert metas[3].pubkey == owner and metas[3].is_signer and not metas[3].is_writable
    print("ok  transfer_checked instruction is laid out correctly")


def test_transfer_checked_amount_is_little_endian() -> None:
    """A big-endian amount would silently transfer a wildly different sum."""
    ix = W.transfer_checked_instruction(
        new_pubkey(), new_pubkey(), new_pubkey(), new_pubkey(), 256, 9
    )
    # 256 is 0x0100, so little-endian puts the low byte (0x00) first.
    assert ix.data[1:9] == b"\x00\x01" + b"\x00" * 6, ix.data[1:9]
    print("ok  transfer_checked amount is little-endian")


def test_transfer_checked_rejects_an_oversized_amount() -> None:
    """u64 cannot hold more; a wrapping amount must fail loudly."""
    for amount in (2**64, -1):
        try:
            W.transfer_checked_instruction(
                new_pubkey(), new_pubkey(), new_pubkey(), new_pubkey(), amount, 6
            )
        except (OverflowError, ValueError):
            continue
        raise AssertionError(f"expected an error for amount {amount}")
    print("ok  transfer_checked refuses amounts outside u64")


def test_create_ata_layout() -> None:
    payer, owner, mint = (new_pubkey() for _ in range(3))
    ix = W.create_ata_instruction(payer, owner, mint)

    assert ix.program_id == W.ASSOCIATED_TOKEN_PROGRAM, ix.program_id
    assert ix.data == bytes([0]), ix.data  # instruction 0 = Create

    metas = ix.accounts
    assert len(metas) == 6, len(metas)
    assert metas[0].pubkey == payer and metas[0].is_signer and metas[0].is_writable
    assert metas[1].pubkey == W.associated_token_address(owner, mint)
    assert metas[2].pubkey == owner and not metas[2].is_signer
    assert metas[3].pubkey == mint
    assert metas[4].pubkey == W.SYSTEM_PROGRAM
    assert metas[5].pubkey == W.TOKEN_PROGRAM
    print("ok  create-ATA instruction is laid out correctly")


def test_associated_token_address_is_deterministic() -> None:
    owner, mint = new_pubkey(), new_pubkey()
    first = W.associated_token_address(owner, mint)
    assert first == W.associated_token_address(owner, mint)
    # A different owner or a different mint must give a different account,
    # otherwise funds could land in the wrong place.
    assert first != W.associated_token_address(new_pubkey(), mint)
    assert first != W.associated_token_address(owner, new_pubkey())
    print("ok  associated token address is deterministic and owner-specific")


# ── safety constants ────────────────────────────────────────────────────


def test_fee_buffer_is_a_sane_fraction_of_a_sol() -> None:
    """Small enough to be worth withdrawing around, large enough to matter."""
    assert 0 < W.SOL_FEE_BUFFER_LAMPORTS < 1_000_000, W.SOL_FEE_BUFFER_LAMPORTS
    assert W.SOL_FEE_BUFFER_LAMPORTS >= 5_000, "must cover at least one signature"
    print("ok  fee buffer is a sane fraction of a SOL")


def test_known_tokens_hold_real_mints() -> None:
    """These are the mints the bot actually trades; a typo here is costly."""
    assert W.KNOWN_TOKENS["USDC"] == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert W.KNOWN_TOKENS["WSOL"] == "So11111111111111111111111111111111111111112"
    for name, mint in W.KNOWN_TOKENS.items():
        assert W.parse_destination(mint) is not None, name
    print("ok  known token names resolve to valid mints")


def main() -> int:
    tests = [
        test_fmt_trims_trailing_zeros,
        test_to_units_has_no_float_drift,
        test_to_units_truncates_excess_precision,
        test_parse_amount_accepts_all_and_max,
        test_parse_amount_accepts_numbers,
        test_parse_amount_refuses_junk,
        test_parse_destination_accepts_a_real_address,
        test_parse_destination_refuses_junk,
        test_parse_destination_refuses_the_wrong_length,
        test_transfer_checked_layout,
        test_transfer_checked_amount_is_little_endian,
        test_transfer_checked_rejects_an_oversized_amount,
        test_create_ata_layout,
        test_associated_token_address_is_deterministic,
        test_fee_buffer_is_a_sane_fraction_of_a_sol,
        test_known_tokens_hold_real_mints,
    ]

    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")

    print()
    if failed:
        print(f"{failed}/{len(tests)} tests FAILED")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
