import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import core.udpsec_identity as udpsec_identity


ROOT = Path(__file__).resolve().parents[1]
SERVER_PRIVATE_NAME = "aismixer_private.pem"
SERVER_PUBLIC_NAME = "aismixer_public.pem"
LEGACY_PRIVATE_NAME = "aismixer_private.key"
PLAIN_CONFIG = {"sec_inputs": []}
UDPSEC_CONFIG = {
    "sec_inputs": [
        {
            "listen_ip": "127.0.0.1",
            "listen_port": 19999,
        }
    ]
}


def _load_private_key(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _load_public_key(path: Path):
    return serialization.load_pem_public_key(path.read_bytes())


def _generate_server_pair(keys_dir: Path):
    return _generate_pair(
        keys_dir,
        private_name=SERVER_PRIVATE_NAME,
        public_name=SERVER_PUBLIC_NAME,
    )


def _generate_pair(
    keys_dir: Path,
    *,
    private_name: str,
    public_name: str,
):
    keys_dir.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_path = keys_dir / private_name
    public_path = keys_dir / public_name
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return SimpleNamespace(
        private_path=private_path,
        public_path=public_path,
    )


@pytest.mark.parametrize(
    "module_path",
    ("core/key_material.py", "core/udpsec_identity.py"),
)
def test_udpsec_identity_core_modules_do_not_import_cli_tool_code(module_path):
    source = (ROOT / module_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert not {
        module
        for module in imported_modules
        if module == "tools" or module.startswith("tools.")
    }


def test_plain_configuration_needs_no_identity_and_creates_no_key_directory(
    tmp_path,
):
    keys_dir = tmp_path / "keys"
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    result = service.ensure_for_configuration(PLAIN_CONFIG)

    assert result is None
    assert not keys_dir.exists()


def test_udpsec_configuration_without_keys_generates_matching_p256_pair(
    tmp_path,
):
    keys_dir = tmp_path / "keys"
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    result = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert result is not None
    assert result.generated is True
    assert result.private_path == keys_dir / SERVER_PRIVATE_NAME
    assert result.public_path == keys_dir / SERVER_PUBLIC_NAME

    private_key = _load_private_key(result.private_path)
    public_key = _load_public_key(result.public_path)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)
    assert isinstance(private_key.curve, ec.SECP256R1)
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)
    assert (
        private_key.public_key().public_numbers()
        == public_key.public_numbers()
    )


def test_complete_existing_pair_is_preserved_byte_for_byte(tmp_path):
    keys_dir = tmp_path / "keys"
    generated = _generate_server_pair(keys_dir)
    original_private = generated.private_path.read_bytes()
    original_public = generated.public_path.read_bytes()
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    result = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert result is not None
    assert result.generated is False
    assert result.private_path == generated.private_path
    assert result.public_path == generated.public_path
    assert generated.private_path.read_bytes() == original_private
    assert generated.public_path.read_bytes() == original_public


@pytest.mark.parametrize("missing_member", ("private", "public"))
def test_incomplete_pair_fails_clearly_and_preserves_existing_member(
    tmp_path,
    missing_member,
):
    keys_dir = tmp_path / "keys"
    generated = _generate_server_pair(keys_dir)
    missing_path = (
        generated.private_path
        if missing_member == "private"
        else generated.public_path
    )
    existing_path = (
        generated.public_path
        if missing_member == "private"
        else generated.private_path
    )
    missing_path.unlink()
    original_existing = existing_path.read_bytes()
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    with pytest.raises(
        udpsec_identity.IncompleteUdpsecServerIdentityError,
        match="identity is incomplete",
    ) as exc_info:
        service.ensure_for_configuration(UDPSEC_CONFIG)

    assert exc_info.value.existing_path == existing_path
    assert exc_info.value.missing_path == missing_path
    assert "Refusing to generate or overwrite" in str(exc_info.value)
    assert existing_path.read_bytes() == original_existing
    assert not missing_path.exists()


def test_sequential_plain_to_udpsec_transition_ensures_identity(tmp_path):
    keys_dir = tmp_path / "keys"
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    plain_result = service.ensure_for_configuration(PLAIN_CONFIG)
    assert plain_result is None
    assert not keys_dir.exists()

    udpsec_result = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert udpsec_result is not None
    assert udpsec_result.generated is True
    assert udpsec_result.private_path.exists()
    assert udpsec_result.public_path.exists()


def test_repeated_udpsec_activation_does_not_replace_identity(tmp_path):
    keys_dir = tmp_path / "keys"
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    first = service.ensure_for_configuration(UDPSEC_CONFIG)
    assert first is not None
    original_private = first.private_path.read_bytes()
    original_public = first.public_path.read_bytes()

    second = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert second is not None
    assert first.generated is True
    assert second.generated is False
    assert second.private_path == first.private_path
    assert second.public_path == first.public_path
    assert second.private_path.read_bytes() == original_private
    assert second.public_path.read_bytes() == original_public


def test_complete_legacy_pair_is_used_without_generating_canonical_identity(
    monkeypatch,
    tmp_path,
):
    canonical_dir = tmp_path / "canonical"
    canonical_pair = udpsec_identity._IdentityPair(
        canonical_dir / SERVER_PRIVATE_NAME,
        canonical_dir / SERVER_PUBLIC_NAME,
    )
    legacy_dir = tmp_path / "legacy"
    legacy_generated = _generate_pair(
        legacy_dir,
        private_name="aismixer_private.key",
        public_name="aismixer_public.pem",
    )
    legacy_pair = udpsec_identity._IdentityPair(
        legacy_generated.private_path,
        legacy_generated.public_path,
    )
    original_private = legacy_generated.private_path.read_bytes()
    original_public = legacy_generated.public_path.read_bytes()
    monkeypatch.setattr(
        udpsec_identity,
        "_default_identity_pairs",
        lambda: (canonical_pair, legacy_pair),
    )
    service = udpsec_identity.UdpsecServerIdentityService()

    result = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert result is not None
    assert result.generated is False
    assert result.private_path == legacy_generated.private_path
    assert result.public_path == legacy_generated.public_path
    assert legacy_generated.private_path.read_bytes() == original_private
    assert legacy_generated.public_path.read_bytes() == original_public
    assert not canonical_dir.exists()


def test_invalid_complete_material_fails_without_overwrite(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    private_path = keys_dir / SERVER_PRIVATE_NAME
    public_path = keys_dir / SERVER_PUBLIC_NAME
    private_path.write_bytes(b"invalid private key")
    public_path.write_bytes(b"invalid public key")
    original_private = private_path.read_bytes()
    original_public = public_path.read_bytes()
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    with pytest.raises(
        udpsec_identity.UdpsecServerIdentityError,
        match="Unable to load the existing UDPSEC server identity",
    ):
        service.ensure_for_configuration(UDPSEC_CONFIG)

    assert private_path.read_bytes() == original_private
    assert public_path.read_bytes() == original_public


def test_mismatched_complete_pair_fails_without_overwrite(tmp_path):
    keys_dir = tmp_path / "keys"
    generated = _generate_server_pair(keys_dir)
    other = _generate_server_pair(tmp_path / "other-keys")
    generated.public_path.write_bytes(other.public_path.read_bytes())
    mismatched_private = generated.private_path.read_bytes()
    mismatched_public = generated.public_path.read_bytes()
    service = udpsec_identity.UdpsecServerIdentityService(keys_dir=keys_dir)

    with pytest.raises(
        udpsec_identity.UdpsecServerIdentityError,
        match="public key does not match its private key",
    ):
        service.ensure_for_configuration(UDPSEC_CONFIG)

    assert generated.private_path.read_bytes() == mismatched_private
    assert generated.public_path.read_bytes() == mismatched_public


def _rerooted_identity_pairs(tmp_path: Path):
    """Return the real default search order with every directory moved.

    The canonical keys directory maps to ``tmp_path / "canonical"``, the
    checkout directory to ``tmp_path / "checkout"`` and every other default
    directory to its own directory under ``tmp_path``, so a test follows the
    production lookup order without touching real key locations.
    """
    canonical_dir = tmp_path / "canonical"
    checkout_dir = tmp_path / "checkout"
    directories = {
        udpsec_identity.UDPSEC_SERVER_KEYS_DIR: canonical_dir,
        ROOT: checkout_dir,
    }

    def reroot(path: Path) -> Path:
        directory = directories.setdefault(
            path.parent,
            tmp_path / f"system-{len(directories)}",
        )
        return directory / path.name

    pairs = tuple(
        udpsec_identity._IdentityPair(
            reroot(pair.private_path),
            reroot(pair.public_path),
        )
        for pair in udpsec_identity._default_identity_pairs()
    )
    assert {
        path.parent
        for pair in pairs
        for path in (pair.private_path, pair.public_path)
    } >= {canonical_dir, checkout_dir}
    return pairs, canonical_dir, checkout_dir


def _service_with_identity_pairs(monkeypatch, pairs):
    monkeypatch.setattr(
        udpsec_identity,
        "_default_identity_pairs",
        lambda: pairs,
    )
    service = udpsec_identity.UdpsecServerIdentityService()
    # Never let a test fall through to the real default key locations.
    assert service._identity_pairs == pairs
    return service


@pytest.mark.parametrize(
    ("present_name", "missing_name"),
    (
        # A fresh checkout has this shape: the repository tracks the server
        # public key but never its private key.
        (SERVER_PUBLIC_NAME, SERVER_PRIVATE_NAME),
        (SERVER_PRIVATE_NAME, SERVER_PUBLIC_NAME),
        (LEGACY_PRIVATE_NAME, SERVER_PUBLIC_NAME),
    ),
)
def test_checkout_half_identity_fails_closed_without_generation(
    monkeypatch,
    tmp_path,
    present_name,
    missing_name,
):
    pairs, canonical_dir, checkout_dir = _rerooted_identity_pairs(tmp_path)
    source = _generate_server_pair(tmp_path / "source")
    member = (
        source.public_path
        if present_name == SERVER_PUBLIC_NAME
        else source.private_path
    )
    checkout_dir.mkdir()
    present_path = checkout_dir / present_name
    present_path.write_bytes(member.read_bytes())
    original_present = present_path.read_bytes()
    service = _service_with_identity_pairs(monkeypatch, pairs)

    with pytest.raises(
        udpsec_identity.IncompleteUdpsecServerIdentityError,
        match="identity is incomplete",
    ) as exc_info:
        service.ensure_for_configuration(UDPSEC_CONFIG)

    assert exc_info.value.existing_path == present_path
    assert exc_info.value.missing_path == checkout_dir / missing_name
    assert present_path.read_bytes() == original_present
    assert list(checkout_dir.iterdir()) == [present_path]
    assert not canonical_dir.exists()


def test_checkout_legacy_key_with_shared_public_key_is_used_without_generation(
    monkeypatch,
    tmp_path,
):
    pairs, canonical_dir, checkout_dir = _rerooted_identity_pairs(tmp_path)
    generated = _generate_pair(
        checkout_dir,
        private_name=LEGACY_PRIVATE_NAME,
        public_name=SERVER_PUBLIC_NAME,
    )
    original_private = generated.private_path.read_bytes()
    original_public = generated.public_path.read_bytes()
    service = _service_with_identity_pairs(monkeypatch, pairs)

    result = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert result is not None
    assert result.generated is False
    assert result.private_path == generated.private_path
    assert result.public_path == generated.public_path
    assert generated.private_path.read_bytes() == original_private
    assert generated.public_path.read_bytes() == original_public
    assert not (checkout_dir / SERVER_PRIVATE_NAME).exists()
    assert not canonical_dir.exists()


def test_checkout_legacy_key_with_mismatched_public_key_fails_closed(
    monkeypatch,
    tmp_path,
):
    pairs, canonical_dir, checkout_dir = _rerooted_identity_pairs(tmp_path)
    generated = _generate_pair(
        checkout_dir,
        private_name=LEGACY_PRIVATE_NAME,
        public_name=SERVER_PUBLIC_NAME,
    )
    other = _generate_server_pair(tmp_path / "other")
    generated.public_path.write_bytes(other.public_path.read_bytes())
    mismatched_private = generated.private_path.read_bytes()
    mismatched_public = generated.public_path.read_bytes()
    service = _service_with_identity_pairs(monkeypatch, pairs)

    with pytest.raises(
        udpsec_identity.UdpsecServerIdentityError,
        match="public key does not match its private key",
    ):
        service.ensure_for_configuration(UDPSEC_CONFIG)

    assert generated.private_path.read_bytes() == mismatched_private
    assert generated.public_path.read_bytes() == mismatched_public
    assert not canonical_dir.exists()


def test_absent_identity_is_generated_in_canonical_directory_not_checkout(
    monkeypatch,
    tmp_path,
):
    pairs, canonical_dir, checkout_dir = _rerooted_identity_pairs(tmp_path)
    service = _service_with_identity_pairs(monkeypatch, pairs)

    result = service.ensure_for_configuration(UDPSEC_CONFIG)

    assert result is not None
    assert result.generated is True
    assert result.private_path == canonical_dir / SERVER_PRIVATE_NAME
    assert result.public_path == canonical_dir / SERVER_PUBLIC_NAME
    assert not checkout_dir.exists()


def test_plain_configuration_ignores_checkout_half_identity(
    monkeypatch,
    tmp_path,
):
    pairs, canonical_dir, checkout_dir = _rerooted_identity_pairs(tmp_path)
    checkout_dir.mkdir()
    lone_public = checkout_dir / SERVER_PUBLIC_NAME
    lone_public.write_bytes(b"public half only")
    service = _service_with_identity_pairs(monkeypatch, pairs)

    assert service.ensure_for_configuration(PLAIN_CONFIG) is None
    assert lone_public.read_bytes() == b"public half only"
    assert list(checkout_dir.iterdir()) == [lone_public]
    assert not canonical_dir.exists()
