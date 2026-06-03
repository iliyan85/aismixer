#!/usr/bin/env python3
"""Generate AISMixer and nmea_sproxy ECDSA P-256 key pairs."""

from __future__ import annotations

import argparse
import base64
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


SERVER_KEYS_DIR = Path("/etc/aismixer/keys")
SERVER_PRIVATE_NAME = "aismixer_private.pem"
SERVER_PUBLIC_NAME = "aismixer_public.pem"

STATION_KEYS_DIR = Path("/etc/nmea_sproxy/keys")
STATION_PRIVATE_NAME = "station_private.pem"
STATION_PUBLIC_NAME = "station_public.pem"
STATION_SERVER_PUBLIC_NAME = "aismixer_public.pem"

PRIVATE_MODE = 0o600
PUBLIC_MODE = 0o644


@dataclass(frozen=True)
class GeneratedKeyPair:
    private_path: Path
    public_path: Path
    compressed_public_b64: str


class KeyFileExistsError(RuntimeError):
    def __init__(self, paths):
        self.paths = tuple(Path(path) for path in paths)
        joined = ", ".join(str(path) for path in self.paths)
        super().__init__(f"Refusing to overwrite existing key file(s): {joined}")


def _write_file(path: Path, data: bytes, mode: int, *, force: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise KeyFileExistsError((path,)) from exc

    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def _serialize_private_key(private_key) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _serialize_public_key(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _compressed_public_b64(public_key) -> str:
    compressed = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return base64.b64encode(compressed).decode("ascii")


def generate_key_pair(
    keys_dir: Path | str,
    private_name: str,
    public_name: str,
    *,
    force: bool = False,
) -> GeneratedKeyPair:
    keys_dir = Path(keys_dir)
    private_path = keys_dir / private_name
    public_path = keys_dir / public_name

    keys_dir.mkdir(parents=True, exist_ok=True)
    existing = [path for path in (private_path, public_path) if path.exists()]
    if existing and not force:
        raise KeyFileExistsError(existing)

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    _write_file(
        private_path,
        _serialize_private_key(private_key),
        PRIVATE_MODE,
        force=force,
    )
    _write_file(
        public_path,
        _serialize_public_key(public_key),
        PUBLIC_MODE,
        force=force,
    )

    return GeneratedKeyPair(
        private_path=private_path,
        public_path=public_path,
        compressed_public_b64=_compressed_public_b64(public_key),
    )


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
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing private/public key files",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate AISMixer server or nmea_sproxy station keys."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="generate server/mixer keys")
    _add_common_options(
        server,
        SERVER_KEYS_DIR,
        SERVER_PRIVATE_NAME,
        SERVER_PUBLIC_NAME,
    )

    station = subparsers.add_parser("station", help="generate station proxy keys")
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
    print("[+] Generated AISMixer server key pair")
    print(f"    Private key: {result.private_path}")
    print(f"    Public key:  {result.public_path}")
    print()
    print("Copy the server public key to each station node as:")
    print(f"    {STATION_KEYS_DIR / STATION_SERVER_PUBLIC_NAME}")
    print("Do not copy or share the server private key.")


def _print_station_guidance(result: GeneratedKeyPair, station_id: str) -> None:
    print("[+] Generated nmea_sproxy station key pair")
    print(f"    Private key: {result.private_path}")
    print(f"    Public key:  {result.public_path}")
    print()
    print("Add this station public key to the server authorized_keys.yaml:")
    print("authorized_clients:")
    print(f"  - name: {station_id}")
    print(f"    pubkey: {result.compressed_public_b64}")
    print()
    print("The station also needs the server public key at:")
    print(f"    {STATION_KEYS_DIR / STATION_SERVER_PUBLIC_NAME}")
    print("This tool does not exchange trust material automatically.")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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
