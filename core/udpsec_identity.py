from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core.key_material import (
    KeyFileExistsError,
    generate_key_pair,
)


UDPSEC_SERVER_KEYS_DIR = Path("/etc/aismixer/keys")
UDPSEC_SERVER_PRIVATE_NAME = "aismixer_private.pem"
UDPSEC_SERVER_PUBLIC_NAME = "aismixer_public.pem"


class UdpsecIdentityConfigError(ValueError):
    """Raised when UDPSEC ingress configuration has not been validated."""


class UdpsecServerIdentityError(RuntimeError):
    """Raised when a required UDPSEC server identity is unavailable."""


class IncompleteUdpsecServerIdentityError(UdpsecServerIdentityError):
    """Raised when only one member of an operator identity pair is present."""

    def __init__(self, existing_path: Path, missing_path: Path) -> None:
        self.existing_path = Path(existing_path)
        self.missing_path = Path(missing_path)
        super().__init__(
            "UDPSEC server identity is incomplete: "
            f"found {self.existing_path}, but {self.missing_path} is missing. "
            "Refusing to generate or overwrite operator key material."
        )


@dataclass(frozen=True, slots=True)
class UdpsecServerIdentity:
    private_path: Path
    public_path: Path
    generated: bool
    private_key: ec.EllipticCurvePrivateKey = field(
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class _IdentityPair:
    private_path: Path
    public_path: Path


def configuration_requires_udpsec(config: Mapping[str, object]) -> bool:
    """Return whether one validated configuration has UDPSEC ingress."""

    if not isinstance(config, Mapping):
        raise UdpsecIdentityConfigError("configuration must be a mapping")
    return sec_inputs_require_udpsec(config.get("sec_inputs", ()))


def sec_inputs_require_udpsec(sec_inputs: object) -> bool:
    """Return whether a validated ``sec_inputs`` collection is non-empty."""

    if isinstance(sec_inputs, (str, bytes)) or not isinstance(
        sec_inputs, Sequence
    ):
        raise UdpsecIdentityConfigError(
            "sec_inputs must be a list of UDPSEC ingress mappings"
        )
    return len(sec_inputs) > 0


def _default_identity_pairs() -> tuple[_IdentityPair, ...]:
    runtime_dir = Path(__file__).resolve().parents[1]
    return (
        _IdentityPair(
            UDPSEC_SERVER_KEYS_DIR / UDPSEC_SERVER_PRIVATE_NAME,
            UDPSEC_SERVER_KEYS_DIR / UDPSEC_SERVER_PUBLIC_NAME,
        ),
        _IdentityPair(
            Path("/etc/aismixer/aismixer_private.key"),
            Path("/etc/aismixer/aismixer_public.pem"),
        ),
        _IdentityPair(
            runtime_dir / "aismixer_private.pem",
            runtime_dir / "aismixer_public.pem",
        ),
        _IdentityPair(
            runtime_dir / "aismixer_private.key",
            runtime_dir / "aismixer_public.pem",
        ),
    )


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_key_file(path: Path, *, label: str) -> None:
    if not path.exists() or not path.is_file():
        raise UdpsecServerIdentityError(
            f"UDPSEC server {label} key path exists but is not a usable file: "
            f"{path}. Refusing to replace operator key material."
        )


def _load_identity_private_key(
    pair: _IdentityPair,
) -> ec.EllipticCurvePrivateKey:
    _validate_key_file(pair.private_path, label="private")
    _validate_key_file(pair.public_path, label="public")

    try:
        private_key = serialization.load_pem_private_key(
            pair.private_path.read_bytes(),
            password=None,
        )
        public_key = serialization.load_pem_public_key(
            pair.public_path.read_bytes()
        )
    except (OSError, TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise UdpsecServerIdentityError(
            "Unable to load the existing UDPSEC server identity at "
            f"{pair.private_path} and {pair.public_path}: {exc}. "
            "Refusing to replace operator key material."
        ) from exc

    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise UdpsecServerIdentityError(
            "UDPSEC server private key must be an EC P-256 key: "
            f"{pair.private_path}. Refusing to replace operator key material."
        )
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise UdpsecServerIdentityError(
            "UDPSEC server public key must be an EC P-256 key: "
            f"{pair.public_path}. Refusing to replace operator key material."
        )
    if private_key.public_key().public_numbers() != public_key.public_numbers():
        raise UdpsecServerIdentityError(
            "UDPSEC server public key does not match its private key: "
            f"{pair.public_path}. Refusing to replace operator key material."
        )
    return private_key


class UdpsecServerIdentityService:
    """Ensure identity before publishing or starting a UDPSEC configuration."""

    def __init__(self, *, keys_dir: Path | str | None = None) -> None:
        if keys_dir is None:
            self._identity_pairs = _default_identity_pairs()
            self._generation_pair = self._identity_pairs[0]
        else:
            generation_pair = _IdentityPair(
                Path(keys_dir) / UDPSEC_SERVER_PRIVATE_NAME,
                Path(keys_dir) / UDPSEC_SERVER_PUBLIC_NAME,
            )
            self._identity_pairs = (generation_pair,)
            self._generation_pair = generation_pair
        self._lock = threading.Lock()

    def ensure_for_configuration(
        self,
        config: Mapping[str, object],
    ) -> UdpsecServerIdentity | None:
        if not configuration_requires_udpsec(config):
            return None
        return self._ensure_required()

    def ensure_for_sec_inputs(
        self,
        sec_inputs: object,
    ) -> UdpsecServerIdentity | None:
        """Ensure identity for an already validated ingress collection."""

        if not sec_inputs_require_udpsec(sec_inputs):
            return None
        return self._ensure_required()

    def _ensure_required(self) -> UdpsecServerIdentity:
        with self._lock:
            existing = self._find_existing_identity()
            if existing is not None:
                private_key = _load_identity_private_key(existing)
                return UdpsecServerIdentity(
                    private_path=existing.private_path,
                    public_path=existing.public_path,
                    generated=False,
                    private_key=private_key,
                )

            generation_dir = self._generation_pair.private_path.parent
            try:
                generation_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                generated = generate_key_pair(
                    generation_dir,
                    self._generation_pair.private_path.name,
                    self._generation_pair.public_path.name,
                )
            except (KeyFileExistsError, OSError) as exc:
                try:
                    raced_identity = self._find_existing_identity()
                except UdpsecServerIdentityError:
                    raise
                if raced_identity is None:
                    raise UdpsecServerIdentityError(
                        "Unable to generate the required UDPSEC server identity: "
                        f"{exc}"
                    ) from exc
                private_key = _load_identity_private_key(raced_identity)
                return UdpsecServerIdentity(
                    private_path=raced_identity.private_path,
                    public_path=raced_identity.public_path,
                    generated=False,
                    private_key=private_key,
                )

            generated_pair = _IdentityPair(
                generated.private_path,
                generated.public_path,
            )
            private_key = _load_identity_private_key(generated_pair)
            return UdpsecServerIdentity(
                private_path=generated.private_path,
                public_path=generated.public_path,
                generated=True,
                private_key=private_key,
            )

    def _find_existing_identity(self) -> _IdentityPair | None:
        pairs = self._identity_pairs
        for index, pair in enumerate(pairs):
            private_present = _path_present(pair.private_path)
            public_present = _path_present(pair.public_path)

            if private_present:
                if not public_present:
                    raise IncompleteUdpsecServerIdentityError(
                        pair.private_path,
                        pair.public_path,
                    )
                return pair

            if public_present:
                shared_public_has_later_private = any(
                    later.public_path == pair.public_path
                    and _path_present(later.private_path)
                    for later in pairs[index + 1 :]
                )
                if shared_public_has_later_private:
                    continue
                raise IncompleteUdpsecServerIdentityError(
                    pair.public_path,
                    pair.private_path,
                )

        return None


udpsec_server_identity_service = UdpsecServerIdentityService()
