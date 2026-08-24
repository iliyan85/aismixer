"""Generic EC key-pair file generation and public-key repair helpers."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


PRIVATE_MODE = 0o600
PUBLIC_MODE = 0o644


@dataclass(frozen=True)
class GeneratedKeyPair:
    private_path: Path
    public_path: Path
    compressed_public_b64: str


@dataclass(frozen=True)
class PublicKeyRepairResult:
    private_path: Path
    public_path: Path
    compressed_public_b64: str
    repaired: bool


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

    with os.fdopen(fd, "wb") as file:
        file.write(data)
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


def _public_key_matches(path: Path, expected_public_key) -> bool:
    try:
        existing_public_key = serialization.load_pem_public_key(path.read_bytes())
    except (OSError, TypeError, ValueError, UnsupportedAlgorithm):
        return False

    return _serialize_public_key(existing_public_key) == _serialize_public_key(
        expected_public_key
    )


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


def repair_public_key(
    keys_dir: Path | str,
    private_name: str,
    public_name: str,
) -> PublicKeyRepairResult:
    keys_dir = Path(keys_dir)
    private_path = keys_dir / private_name
    public_path = keys_dir / public_name

    private_key = serialization.load_pem_private_key(
        private_path.read_bytes(),
        password=None,
    )
    public_key = private_key.public_key()
    compressed_public_b64 = _compressed_public_b64(public_key)
    public_matches = _public_key_matches(public_path, public_key)

    os.chmod(private_path, PRIVATE_MODE)
    if public_matches:
        os.chmod(public_path, PUBLIC_MODE)
    else:
        _write_file(
            public_path,
            _serialize_public_key(public_key),
            PUBLIC_MODE,
            force=True,
        )

    return PublicKeyRepairResult(
        private_path=private_path,
        public_path=public_path,
        compressed_public_b64=compressed_public_b64,
        repaired=not public_matches,
    )
