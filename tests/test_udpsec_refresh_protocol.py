"""UDPSEC revision 3 epoch-refresh wire codecs and transcript digests.

Structural/codec coverage for `core.udpsec_protocol`'s four refresh control
messages and `core.udpsec_crypto`'s refresh transcript digests + key
schedule. Server/client state-machine behaviour is covered by
tests/test_udpsec_refresh.py.
"""

import base64
import os

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import core.udpsec_crypto as udpsec_crypto
import core.udpsec_protocol as p


def _epoch_pub():
    priv = udpsec_crypto.generate_ephemeral_private_key()
    return udpsec_crypto.serialize_ephemeral_public_key(priv.public_key())


def _init_kwargs(**changes):
    values = dict(
        station_id="boat_001",
        transaction_id=os.urandom(32),
        parent_generation=0,
        next_generation=1,
        epoch_random=os.urandom(32),
        epoch_public_key=_epoch_pub(),
        refresh_signature=bytes.fromhex("3006020101020101"),
        timestamp=1,
    )
    values.update(changes)
    return values


def _short_kwargs(**changes):
    values = dict(
        station_id="boat_001",
        transaction_id=os.urandom(32),
        next_generation=1,
        timestamp=7,
    )
    values.update(changes)
    return values


# --- DATA epoch selector -----------------------------------------------------


@pytest.mark.parametrize(
    ("generation", "selector"),
    ((0, 0), (1, 1), (255, 255), (256, 0), (515, 3), ((1 << 32) - 1, 255)),
)
def test_epoch_selector_is_low_byte(generation, selector):
    assert p.epoch_selector_for_generation(generation) == selector


@pytest.mark.parametrize("bad", (True, 1.5, -1, 1 << 32))
def test_epoch_selector_rejects_invalid_generation(bad):
    with pytest.raises((TypeError, ValueError)):
        p.epoch_selector_for_generation(bad)


def test_data_aad_binds_full_generation_not_only_selector():
    loc = os.urandom(16)
    assert p.build_data_aad(loc, 1) != p.build_data_aad(loc, 257)
    assert p.build_data_aad(loc, 5) == p.DATA_PREFIX + loc + b"\x00\x00\x00\x05"


# --- refresh_init / refresh_reply ------------------------------------------


def test_refresh_init_round_trip():
    kwargs = _init_kwargs()
    msg = p.build_refresh_init_message(**kwargs)
    assert msg["type"] == p.REFRESH_INIT_TYPE
    parsed = p.parse_refresh_init_message(msg)
    assert parsed.station_id == "boat_001"
    assert parsed.transaction_id == kwargs["transaction_id"]
    assert parsed.parent_generation == 0
    assert parsed.next_generation == 1
    assert parsed.client_epoch_random == kwargs["epoch_random"]
    assert parsed.client_epoch_public_key == kwargs["epoch_public_key"]
    assert parsed.refresh_signature == kwargs["refresh_signature"]


def test_refresh_reply_round_trip():
    kwargs = _init_kwargs()
    msg = p.build_refresh_reply_message(**kwargs)
    assert msg["type"] == p.REFRESH_REPLY_TYPE
    parsed = p.parse_refresh_reply_message(msg)
    assert parsed.server_epoch_public_key == kwargs["epoch_public_key"]
    assert parsed.parent_generation == 0 and parsed.next_generation == 1


@pytest.mark.parametrize(
    "mutate",
    (
        lambda m: {**m, "extra": 1},
        lambda m: {k: v for k, v in m.items() if k != "txn"},
        lambda m: {**m, "type": "refresh_reply"},
        lambda m: {**m, "parent_gen": 2},
        lambda m: {**m, "next_gen": 3},
        lambda m: {**m, "parent_gen": True},
        lambda m: {**m, "txn": base64.b64encode(os.urandom(31)).decode()},
        lambda m: {**m, "epoch_pub": base64.b64encode(os.urandom(33)).decode()},
        lambda m: {**m, "epoch_random": base64.b64encode(b"x" * 31).decode()},
        lambda m: {**m, "refresh_sig": ""},
        lambda m: {**m, "timestamp": 1.5},
        lambda m: {**m, "timestamp": True},
        lambda m: {**m, "epoch_pub": "not base64!!"},
    ),
)
def test_refresh_init_rejects_malformed(mutate):
    msg = p.build_refresh_init_message(**_init_kwargs())
    with pytest.raises((ValueError, TypeError)):
        p.parse_refresh_init_message(mutate(msg))


def test_refresh_init_next_gen_must_be_parent_plus_one():
    with pytest.raises(ValueError):
        p.build_refresh_init_message(**_init_kwargs(next_generation=2))


# --- refresh_confirm / refresh_ack ----------------------------------------


def test_refresh_confirm_round_trip():
    kwargs = _short_kwargs()
    msg = p.build_refresh_confirm_message(**kwargs)
    assert set(msg) == {"type", "source_id", "txn", "next_gen", "timestamp"}
    parsed = p.parse_refresh_confirm_message(msg)
    assert parsed.transaction_id == kwargs["transaction_id"]
    assert parsed.next_generation == 1


def test_refresh_ack_round_trip():
    msg = p.build_refresh_ack_message(**_short_kwargs())
    parsed = p.parse_refresh_ack_message(msg)
    assert parsed.next_generation == 1


@pytest.mark.parametrize(
    "mutate",
    (
        lambda m: {**m, "extra": 1},
        lambda m: {**m, "type": "refresh_ack"},
        lambda m: {**m, "next_gen": -1},
        lambda m: {**m, "next_gen": True},
        lambda m: {**m, "txn": base64.b64encode(os.urandom(16)).decode()},
        lambda m: {**m, "timestamp": None},
    ),
)
def test_refresh_confirm_rejects_malformed(mutate):
    msg = p.build_refresh_confirm_message(**_short_kwargs())
    with pytest.raises((ValueError, TypeError)):
        p.parse_refresh_confirm_message(mutate(msg))


def test_confirm_and_ack_are_disambiguated_by_type():
    confirm = p.build_refresh_confirm_message(**_short_kwargs())
    with pytest.raises(ValueError):
        p.parse_refresh_ack_message(confirm)


# --- transcript digests + key schedule ------------------------------------


def _refresh_common():
    return dict(
        protocol_version=p.UDPSEC_PROTOCOL_VERSION,
        station_id="boat_001",
        session_locator=os.urandom(16),
        parent_generation=3,
        next_generation=4,
        transaction_id=os.urandom(32),
        client_epoch_random=os.urandom(32),
        client_epoch_public_key=_epoch_pub(),
    )


def test_refresh_digests_are_domain_separated_from_establishment():
    common = _refresh_common()
    init_digest = udpsec_crypto.build_refresh_init_digest(**common)
    est_digest = udpsec_crypto.build_client_auth_digest(
        protocol_version=common["protocol_version"],
        station_id=common["station_id"],
        timestamp=common["parent_generation"],
        client_random=common["client_epoch_random"],
        client_ephemeral_public_key=common["client_epoch_public_key"],
    )
    assert init_digest != est_digest


def test_refresh_init_reply_transcript_chain_and_keys():
    common = _refresh_common()
    id_priv = ec.generate_private_key(ec.SECP256R1())
    client_epriv = udpsec_crypto.generate_ephemeral_private_key()
    server_epriv = udpsec_crypto.generate_ephemeral_private_key()
    common["client_epoch_public_key"] = (
        udpsec_crypto.serialize_ephemeral_public_key(client_epriv.public_key())
    )
    server_pub = udpsec_crypto.serialize_ephemeral_public_key(
        server_epriv.public_key()
    )
    server_random = os.urandom(32)

    init_digest = udpsec_crypto.build_refresh_init_digest(**common)
    client_sig = udpsec_crypto.sign_transcript_digest(id_priv, init_digest)
    assert udpsec_crypto.verify_transcript_signature(
        id_priv.public_key(), client_sig, init_digest
    )

    reply_digest = udpsec_crypto.build_refresh_reply_digest(
        **common,
        client_refresh_signature=client_sig,
        server_epoch_random=server_random,
        server_epoch_public_key=server_pub,
    )
    server_sig = udpsec_crypto.sign_transcript_digest(id_priv, reply_digest)

    transcript = udpsec_crypto.build_refresh_transcript_hash(
        **common,
        client_refresh_signature=client_sig,
        server_epoch_random=server_random,
        server_epoch_public_key=server_pub,
        server_refresh_signature=server_sig,
    )

    ss_client = udpsec_crypto.derive_ephemeral_shared_secret(
        client_epriv, udpsec_crypto.parse_ephemeral_public_key(server_pub)
    )
    ss_server = udpsec_crypto.derive_ephemeral_shared_secret(
        server_epriv,
        udpsec_crypto.parse_ephemeral_public_key(
            common["client_epoch_public_key"]
        ),
    )
    assert ss_client == ss_server

    km_client = udpsec_crypto.derive_refresh_epoch_key_material(
        ss_client, transcript
    )
    km_server = udpsec_crypto.derive_refresh_epoch_key_material(
        ss_server, transcript
    )
    assert km_client == km_server
    assert km_client.client_to_server_key != km_client.server_to_client_key

    # Domain-separated from the establishment schedule for identical inputs.
    est = udpsec_crypto.derive_session_key_material(ss_client, transcript)
    assert est.client_to_server_key != km_client.client_to_server_key


@pytest.mark.parametrize(
    "field",
    (
        "protocol_version",
        "session_locator",
        "parent_generation",
        "next_generation",
        "transaction_id",
        "client_epoch_random",
        "client_epoch_public_key",
    ),
)
def test_refresh_init_digest_binds_every_field(field):
    common = _refresh_common()
    base = udpsec_crypto.build_refresh_init_digest(**common)
    altered = dict(common)
    if field == "protocol_version":
        pytest.skip("only one protocol version is accepted")
    elif field in ("parent_generation", "next_generation"):
        altered["parent_generation"] = common["parent_generation"] + 10
        altered["next_generation"] = altered["parent_generation"] + 1
    elif field == "session_locator":
        altered[field] = os.urandom(16)
    else:
        altered[field] = os.urandom(len(common[field]))
    assert udpsec_crypto.build_refresh_init_digest(**altered) != base
