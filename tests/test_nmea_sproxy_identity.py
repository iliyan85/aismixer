import importlib.util
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"


def load_proxy_module():
    module_name = "nmea_sproxy_identity_tests"
    previous_meta_cleaner = sys.modules.pop("meta_cleaner", None)
    previous_module = sys.modules.pop(module_name, None)
    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            NMEA_SPROXY_DIR / "nmea_sproxy.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))
        sys.modules.pop("meta_cleaner", None)
        if previous_meta_cleaner is not None:
            sys.modules["meta_cleaner"] = previous_meta_cleaner
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


def _private_bytes(private_key):
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_bytes(public_key):
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _write_private(path, private_key=None):
    private_key = private_key or ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_private_bytes(private_key))
    return private_key


def _write_pair(private_path, public_path, private_key=None):
    private_key = _write_private(private_path, private_key)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(_public_bytes(private_key.public_key()))
    return private_key


def _load_private(path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _load_public(path):
    return serialization.load_pem_public_key(path.read_bytes())


def _canonical_paths(proxy, monkeypatch, tmp_path):
    keys_dir = tmp_path / "etc" / "nmea_sproxy" / "keys"
    private_path = keys_dir / "station_private.pem"
    public_path = keys_dir / "station_public.pem"
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PRIVATE_KEY_PATH",
        str(private_path),
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PUBLIC_KEY_PATH",
        str(public_path),
        raising=False,
    )
    return private_path, public_path


def _config(output_type, private_path=None):
    config = {"output": {"type": output_type}}
    if private_path is not None:
        config["station_private_key"] = os.fspath(private_path)
    return config


def _assert_identity_error(proxy, match=None):
    return pytest.raises(proxy.StationIdentityError, match=match)


def test_plain_output_does_not_inspect_or_create_station_identity(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    private_path, public_path = _canonical_paths(proxy, monkeypatch, tmp_path)

    def fail_identity_access(*_args, **_kwargs):
        raise AssertionError("plain UDP must not inspect or create station identity")

    monkeypatch.setattr(proxy, "_path_present", fail_identity_access)
    monkeypatch.setattr(proxy, "generate_key_pair", fail_identity_access)

    result = proxy.ensure_station_identity(_config("udp", private_path))

    assert result is None
    assert not private_path.parent.exists()
    assert not private_path.exists()
    assert not public_path.exists()


def test_udpsec_without_canonical_keys_generates_matching_p256_pair(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    private_path, public_path = _canonical_paths(proxy, monkeypatch, tmp_path)

    result = proxy.ensure_station_identity(
        _config("udpsec", private_path),
    )

    assert result.generated is True
    assert Path(result.private_path) == private_path
    assert Path(result.public_path) == public_path
    loaded_private = _load_private(private_path)
    loaded_public = _load_public(public_path)
    assert isinstance(loaded_private, ec.EllipticCurvePrivateKey)
    assert isinstance(loaded_private.curve, ec.SECP256R1)
    assert isinstance(loaded_public, ec.EllipticCurvePublicKey)
    assert isinstance(loaded_public.curve, ec.SECP256R1)
    assert (
        result.private_key.public_key().public_numbers()
        == loaded_private.public_key().public_numbers()
        == loaded_public.public_numbers()
    )


def test_udpsec_complete_canonical_pair_is_preserved_byte_for_byte(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    private_path, public_path = _canonical_paths(proxy, monkeypatch, tmp_path)
    _write_pair(private_path, public_path)
    original_private = private_path.read_bytes()
    original_public = public_path.read_bytes()

    result = proxy.ensure_station_identity(
        _config("udpsec", private_path),
    )

    assert result.generated is False
    assert Path(result.private_path) == private_path
    assert Path(result.public_path) == public_path
    assert private_path.read_bytes() == original_private
    assert public_path.read_bytes() == original_public


@pytest.mark.parametrize("missing_member", ("private", "public"))
def test_udpsec_partial_canonical_pair_fails_without_mutation(
    monkeypatch,
    tmp_path,
    missing_member,
):
    proxy = load_proxy_module()
    private_path, public_path = _canonical_paths(proxy, monkeypatch, tmp_path)
    _write_pair(private_path, public_path)
    missing_path, existing_path = (
        (private_path, public_path)
        if missing_member == "private"
        else (public_path, private_path)
    )
    missing_path.unlink()
    original_existing = existing_path.read_bytes()

    with _assert_identity_error(proxy, r"(?i)(incomplete|missing)"):
        proxy.ensure_station_identity(_config("udpsec", private_path))

    assert existing_path.read_bytes() == original_existing
    assert not missing_path.exists()


def test_udpsec_invalid_canonical_pair_fails_without_mutation(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    private_path, public_path = _canonical_paths(proxy, monkeypatch, tmp_path)
    private_path.parent.mkdir(parents=True)
    private_path.write_bytes(b"invalid private key")
    public_path.write_bytes(b"invalid public key")
    original_private = private_path.read_bytes()
    original_public = public_path.read_bytes()

    with _assert_identity_error(proxy, r"(?i)(identity|private key|public key)"):
        proxy.ensure_station_identity(_config("udpsec", private_path))

    assert private_path.read_bytes() == original_private
    assert public_path.read_bytes() == original_public


def test_udpsec_mismatched_canonical_pair_fails_without_mutation(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    private_path, public_path = _canonical_paths(proxy, monkeypatch, tmp_path)
    _write_pair(private_path, public_path)
    other_private = ec.generate_private_key(ec.SECP256R1())
    public_path.write_bytes(_public_bytes(other_private.public_key()))
    original_private = private_path.read_bytes()
    original_public = public_path.read_bytes()

    with _assert_identity_error(proxy, r"(?i)(does not match|mismatch)"):
        proxy.ensure_station_identity(_config("udpsec", private_path))

    assert private_path.read_bytes() == original_private
    assert public_path.read_bytes() == original_public


def test_existing_legacy_private_key_is_used_without_canonical_generation(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    canonical_private, canonical_public = _canonical_paths(
        proxy,
        monkeypatch,
        tmp_path,
    )
    legacy_private = canonical_private.with_name("station_private.key")
    expected_private = _write_private(legacy_private)
    canonical_public.parent.mkdir(parents=True, exist_ok=True)
    canonical_public.write_bytes(_public_bytes(expected_private.public_key()))
    original_private = legacy_private.read_bytes()
    original_public = canonical_public.read_bytes()

    result = proxy.ensure_station_identity(
        _config("udpsec", legacy_private),
    )

    assert result.generated is False
    assert Path(result.private_path) == legacy_private
    assert result.public_path is None
    assert (
        result.private_key.public_key().public_numbers()
        == expected_private.public_key().public_numbers()
    )
    assert legacy_private.read_bytes() == original_private
    assert canonical_public.read_bytes() == original_public
    assert not canonical_private.exists()


def test_valid_custom_private_key_remains_operator_owned(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    canonical_private, canonical_public = _canonical_paths(
        proxy,
        monkeypatch,
        tmp_path,
    )
    custom_private = tmp_path / "operator" / "identity.pem"
    _write_private(custom_private)
    original_private = custom_private.read_bytes()

    result = proxy.ensure_station_identity(
        _config("udpsec", custom_private),
    )

    assert result.generated is False
    assert Path(result.private_path) == custom_private
    assert result.public_path is None
    assert custom_private.read_bytes() == original_private
    assert not custom_private.with_name("station_public.pem").exists()
    assert not custom_private.with_suffix(".pub").exists()
    assert not canonical_private.exists()
    assert not canonical_public.exists()


@pytest.mark.parametrize(
    ("material", "message"),
    [
        (None, r"(?i)(missing|not found|does not exist)"),
        (b"not a private key", r"(?i)(invalid|unable to load|private key)"),
    ],
)
def test_missing_or_invalid_custom_private_key_fails_without_generation_or_repair(
    monkeypatch,
    tmp_path,
    material,
    message,
):
    proxy = load_proxy_module()
    canonical_private, canonical_public = _canonical_paths(
        proxy,
        monkeypatch,
        tmp_path,
    )
    custom_private = tmp_path / "operator" / "custom-private.key"
    if material is not None:
        custom_private.parent.mkdir(parents=True)
        custom_private.write_bytes(material)

    with _assert_identity_error(proxy, message):
        proxy.ensure_station_identity(_config("udpsec", custom_private))

    if material is None:
        assert not custom_private.exists()
    else:
        assert custom_private.read_bytes() == material
    assert not custom_private.with_name("station_public.pem").exists()
    assert not custom_private.with_suffix(".pub").exists()
    assert not canonical_private.exists()
    assert not canonical_public.exists()


def test_load_peer_public_key_accepts_p256_trust_material(tmp_path):
    proxy = load_proxy_module()
    peer_path = tmp_path / "trust" / "aismixer_public.pem"
    peer_path.parent.mkdir(parents=True)
    expected = ec.generate_private_key(ec.SECP256R1()).public_key()
    peer_path.write_bytes(_public_bytes(expected))
    original = peer_path.read_bytes()

    loaded = proxy.load_peer_public_key(peer_path)

    assert isinstance(loaded, ec.EllipticCurvePublicKey)
    assert isinstance(loaded.curve, ec.SECP256R1)
    assert loaded.public_numbers() == expected.public_numbers()
    assert peer_path.read_bytes() == original


@pytest.mark.parametrize(
    ("case", "write_material"),
    [
        ("missing", None),
        ("malformed", lambda: b"not a PEM public key"),
        (
            "rsa",
            lambda: _public_bytes(
                rsa.generate_private_key(
                    public_exponent=65537,
                    key_size=2048,
                ).public_key()
            ),
        ),
        (
            "wrong-curve",
            lambda: _public_bytes(
                ec.generate_private_key(ec.SECP384R1()).public_key()
            ),
        ),
    ],
)
def test_missing_invalid_or_incompatible_peer_trust_fails_clearly(
    tmp_path,
    case,
    write_material,
):
    proxy = load_proxy_module()
    peer_path = tmp_path / "trust" / f"{case}.pem"
    original = None
    if write_material is not None:
        peer_path.parent.mkdir(parents=True)
        original = write_material()
        peer_path.write_bytes(original)

    with pytest.raises(proxy.PeerTrustError) as exc_info:
        proxy.load_peer_public_key(peer_path)

    message = str(exc_info.value).lower()
    assert "public key" in message
    assert str(peer_path).lower() in message
    if original is None:
        assert not peer_path.exists()
    else:
        assert peer_path.read_bytes() == original
