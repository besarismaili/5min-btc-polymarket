#!/usr/bin/env python3
"""setup_creds.py — one-time Polymarket CLOB API credential derivation.

Derives (or re-derives) L2 API credentials (key/secret/passphrase) from an
L1 wallet private key using py-clob-client's `create_or_derive_api_creds()`.
This is a one-shot setup step: run it once per wallet, then use
`--write-env` to persist the resulting API creds into `.env` for `bot.py`
to pick up via python-dotenv.

The private key is NEVER printed or written anywhere by this script — it is
only used in-memory to build the ClobClient. Only the derived API key,
secret and passphrase are shown/written.

Usage:
    python setup_creds.py                          # read POLY_PRIVATE_KEY from env/.env, print creds
    python setup_creds.py --write-env               # also write/update .env
    python setup_creds.py --private-key 0xabc...    # pass the key explicitly instead of via env

Env vars used (see .env.example):
    POLY_PRIVATE_KEY      wallet private key (required, unless --private-key given)
    POLY_FUNDER_ADDRESS   optional funder/proxy wallet address (Polymarket proxy wallets)
    POLY_SIGNATURE_TYPE   optional signature type (default 1)
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("setup_creds")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
ENV_KEYS = ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Derive Polymarket CLOB API credentials.")
    parser.add_argument(
        "--private-key",
        default=None,
        help="Wallet private key (0x...). If omitted, read from POLY_PRIVATE_KEY env/.env.",
    )
    parser.add_argument(
        "--funder",
        default=None,
        help="Funder/proxy wallet address. If omitted, read from POLY_FUNDER_ADDRESS env/.env.",
    )
    parser.add_argument(
        "--signature-type",
        type=int,
        default=None,
        help="Signature type (default: POLY_SIGNATURE_TYPE env or 1).",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write/update POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE in .env.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env file to update (default: ./.env).",
    )
    return parser.parse_args()


def resolve_private_key(cli_value: str | None) -> str:
    private_key = cli_value or os.environ.get("POLY_PRIVATE_KEY")
    if not private_key:
        raise SystemExit(
            "No private key found. Set POLY_PRIVATE_KEY in .env/env, or pass --private-key."
        )
    return private_key


def derive_creds(private_key: str, funder: str | None, signature_type: int):
    """Build a ClobClient and derive (or create) API creds. Returns an ApiCreds object."""
    from py_clob_client.client import ClobClient

    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=signature_type,
        funder=funder,
    )
    return client.create_or_derive_api_creds()


def upsert_env_file(path: Path, values: dict[str, str]) -> None:
    """Idempotently add/replace KEY=VALUE lines in an .env file, preserving other content.

    Creates the file if missing. Restricts permissions to 600 (owner read/write only)
    since it holds secrets.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text().splitlines()

    remaining = dict(values)
    updated_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                updated_lines.append(f"{key}={remaining.pop(key)}")
                continue
        updated_lines.append(line)

    # Append any keys that weren't already present.
    if remaining:
        if updated_lines and updated_lines[-1].strip() != "":
            updated_lines.append("")
        for key, value in remaining.items():
            updated_lines.append(f"{key}={value}")

    path.write_text("\n".join(updated_lines) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # chmod 600


def main() -> None:
    load_dotenv()
    args = parse_args()

    private_key = resolve_private_key(args.private_key)
    funder = args.funder or os.environ.get("POLY_FUNDER_ADDRESS") or None
    signature_type = (
        args.signature_type
        if args.signature_type is not None
        else int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
    )

    log.info("Deriving API credentials from CLOB (chain_id=%s)...", CHAIN_ID)
    creds = derive_creds(private_key, funder, signature_type)

    print("Derived Polymarket CLOB API credentials:")
    print(f"  POLY_API_KEY={creds.api_key}")
    print(f"  POLY_API_SECRET={creds.api_secret}")
    print(f"  POLY_API_PASSPHRASE={creds.api_passphrase}")

    if args.write_env:
        env_path = Path(args.env_file)
        upsert_env_file(
            env_path,
            {
                "POLY_API_KEY": creds.api_key,
                "POLY_API_SECRET": creds.api_secret,
                "POLY_API_PASSPHRASE": creds.api_passphrase,
            },
        )
        log.info("Wrote credentials to %s (chmod 600).", env_path)
    else:
        log.info("Re-run with --write-env to persist these into .env.")


if __name__ == "__main__":
    main()
