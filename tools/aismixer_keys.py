#!/usr/bin/env python3
"""Generate or repair aismixer and nmea_sproxy ECDSA P-256 key pairs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.key_material import (  # noqa: E402
    GeneratedKeyPair,
    KeyFileExistsError,
    PRIVATE_MODE,
    PUBLIC_MODE,
    PublicKeyRepairResult,
    _compressed_public_b64,
    generate_key_pair,
    repair_public_key,
)


SERVER_KEYS_DIR = Path("/etc/aismixer/keys")
SERVER_PRIVATE_NAME = "aismixer_private.pem"
SERVER_PUBLIC_NAME = "aismixer_public.pem"

STATION_KEYS_DIR = Path("/etc/nmea_sproxy/keys")
STATION_PRIVATE_NAME = "station_private.pem"
STATION_PUBLIC_NAME = "station_public.pem"
STATION_SERVER_PUBLIC_NAME = "aismixer_public.pem"

def _add_common_options(parser, default_dir: Path, private_name: str, public_name: str):
    parser.add_argument(
        "--keys-dir",
        default=str(default_dir),
        help=f"target key directory (default: {default_dir})",
    )
    parser.add_argument(
        "--private-name",
        default=private_name,
        help=f"private key filename (default: {private_name})",
    )
    parser.add_argument(
        "--public-name",
        default=public_name,
        help=f"public key filename (default: {public_name})",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing private/public key files",
    )
    action.add_argument(
        "--repair-public",
        action="store_true",
        help="derive and repair only the public key from the existing private key",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or repair aismixer server or nmea_sproxy station keys."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="manage server/mixer keys")
    _add_common_options(
        server,
        SERVER_KEYS_DIR,
        SERVER_PRIVATE_NAME,
        SERVER_PUBLIC_NAME,
    )

    station = subparsers.add_parser("station", help="manage station proxy keys")
    _add_common_options(
        station,
        STATION_KEYS_DIR,
        STATION_PRIVATE_NAME,
        STATION_PUBLIC_NAME,
    )
    station.add_argument(
        "--station-id",
        default="boat_001",
        help="station id shown in operator guidance (default: boat_001)",
    )

    return parser


def _print_server_guidance(result: GeneratedKeyPair) -> None:
    print("[+] Generated aismixer server key pair")
    print(f"    Private key: {result.private_path}")
    print(f"    Public key:  {result.public_path}")
    print()
    print("Copy the server public key to each station node as:")
    print(f"    {STATION_KEYS_DIR / STATION_SERVER_PUBLIC_NAME}")
    print("Do not copy or share the server private key.")


def _print_station_operator_guidance(result, station_id: str) -> None:
    print("Add this station public key to the server authorized_keys.yaml:")
    print("authorized_clients:")
    print(f"  - name: {station_id}")
    print(f"    pubkey: {result.compressed_public_b64}")
    print()
    print("The station also needs the server public key at:")
    print(f"    {STATION_KEYS_DIR / STATION_SERVER_PUBLIC_NAME}")
    print("This tool does not exchange trust material automatically.")


def _print_station_guidance(result: GeneratedKeyPair, station_id: str) -> None:
    print("[+] Generated nmea_sproxy station key pair")
    print(f"    Private key: {result.private_path}")
    print(f"    Public key:  {result.public_path}")
    print()
    _print_station_operator_guidance(result, station_id)


def _print_repair_status(result: PublicKeyRepairResult, key_owner: str) -> None:
    if result.repaired:
        print(f"[+] Repaired {key_owner} public key")
    else:
        print(f"[=] {key_owner} public key already matches private key; no repair needed.")
    print(f"    Private key: {result.private_path}")
    print(f"    Public key:  {result.public_path}")


def _print_server_repair_guidance(result: PublicKeyRepairResult) -> None:
    _print_repair_status(result, "aismixer server")


def _print_station_repair_guidance(
    result: PublicKeyRepairResult,
    station_id: str,
) -> None:
    _print_repair_status(result, "nmea_sproxy station")
    print()
    _print_station_operator_guidance(result, station_id)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.repair_public:
        try:
            result = repair_public_key(
                args.keys_dir,
                args.private_name,
                args.public_name,
            )
        except (OSError, TypeError, ValueError, UnsupportedAlgorithm) as exc:
            print(f"[!] Unable to repair public key: {exc}", file=sys.stderr)
            return 1

        if args.command == "server":
            _print_server_repair_guidance(result)
        elif args.command == "station":
            _print_station_repair_guidance(result, args.station_id)
        return 0

    try:
        result = generate_key_pair(
            args.keys_dir,
            args.private_name,
            args.public_name,
            force=args.force,
        )
    except KeyFileExistsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        print("    Re-run with --force to overwrite.", file=sys.stderr)
        return 1

    if args.command == "server":
        _print_server_guidance(result)
    elif args.command == "station":
        _print_station_guidance(result, args.station_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
