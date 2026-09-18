"""`nmea_sproxy` UDPSEC V2 client-side path-migration choreography --
acceptance (Major Prompt 5, including both corrective audit rounds).

Major Prompt 4 built the server-side A -> B path migration primitives
(candidate discovery, PATH_CHALLENGE / PATH_RESPONSE / PATH_ACK,
return-routability proof, atomic commit) but left `nmea_sproxy` unchanged.
Major Prompt 5 makes the client migration-aware: it answers an authenticated
PATH_CHALLENGE with exactly one PATH_RESPONSE, and treats a matching
authenticated PATH_ACK as proof the server is alive and has committed this
exact session's migration.

The corrective work in this file targets F1: a PATH_ACK may grant fresh
liveness ONLY when it matches a live, unexpired, unconsumed, same-epoch
client-side proof that this exact client actually established by answering
a PATH_CHALLENGE -- never merely because its `path_generation` happens to be
numerically greater than one seen before. A historically-valid but delayed
PATH_ACK, delivered for the first time after its proof has expired, must not
be able to reach into the future and supersede an unrelated, later
keepalive ping.

Three layers of coverage:

  * Direct, protocol-visible tests of `_try_handle_path_message` (real
    AES-GCM, no sockets) covering the bounded client-side proof state
    machine: new/duplicate/retry/stale challenge classification, one-shot
    ACK consumption, exact token/generation/epoch/deadline matching, and
    the ping-context correlation between a challenge's response and a
    later ACK.
  * Send-failure regressions for immediate supersession and localized
    error logging on strict console encodings.
  * End-to-end tests through the REAL `forward_loop()` (a scripted fake
    "server" socket, an injected monotonic clock, no sleeps) proving the
    canonical lost-PONG/NAT-rebind scenario, refresh-deadline independence,
    peer-timeout reanchoring, and ping correlation across receive deadlines.
"""

import base64
import builtins
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import core.udpsec_protocol as udpsec_protocol

from test_secure_udp_helpers import load_proxy_module  # noqa: E402

STATION_ID = "boat_001"
REMOTE_ADDR = ("192.0.2.50", 19999)
OTHER_ADDR = ("203.0.113.9", 4444)


@pytest.fixture
def proxy():
    return load_proxy_module()


@pytest.fixture
def keys():
    return {
        "locator": b"M" * 16,
        "c2s": AESGCM.generate_key(bit_length=256),
        "s2c": AESGCM.generate_key(bit_length=256),
    }


def _epochs(proxy, keys):
    km = proxy.SessionKeyMaterial(
        client_to_server_key=keys["c2s"], server_to_client_key=keys["s2c"]
    )
    return proxy._ClientEpochSet(keys["locator"], km)


def _challenge_packet(
    proxy, keys, *, token, generation, station_id=STATION_ID, key=None,
    epoch_generation=0,
):
    key = keys["s2c"] if key is None else key
    message = udpsec_protocol.build_path_challenge_message(
        station_id=station_id,
        challenge_token=token,
        path_generation=generation,
        timestamp=1,
    )
    return proxy.encrypt_secure_json_message(
        message, key, keys["locator"], epoch_generation
    )


def _ack_packet(
    proxy, keys, *, token, generation, station_id=STATION_ID, key=None,
    epoch_generation=0,
):
    key = keys["s2c"] if key is None else key
    message = udpsec_protocol.build_path_ack_message(
        station_id=station_id,
        challenge_token=token,
        path_generation=generation,
        timestamp=1,
    )
    return proxy.encrypt_secure_json_message(
        message, key, keys["locator"], epoch_generation
    )


class _RecordingSock:
    """Captures every outbound datagram, decrypted under the client's
    current client->server key, as (addr, decrypted_message_dict).

    When `epochs` is given, the CURRENT epoch's key/generation are read
    fresh on every call (matching what `on_challenge` just used to
    encrypt) so this stays correct across an in-session epoch refresh;
    otherwise it falls back to the fixed establishment-epoch key/
    generation 0, sufficient for every test that never refreshes."""

    def __init__(self, proxy, keys, epochs=None):
        self.proxy = proxy
        self.keys = keys
        self.epochs = epochs
        self.sent = []

    def sendto(self, data, addr):
        locator, _selector, nonce, ciphertext = udpsec_protocol.parse_data_packet(
            data
        )
        if self.epochs is not None:
            key = self.epochs.client_to_server_key
            generation = self.epochs.generation
        else:
            key = self.keys["c2s"]
            generation = 0
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext, self.proxy.build_data_aad(locator, generation)
        )
        self.sent.append((addr, json.loads(plaintext)))


class _FailingSock:
    """A sock whose sendto() always raises OSError -- simulates a transient
    local send failure (F3)."""

    def __init__(self, message="network is unreachable"):
        self.message = message

    def sendto(self, data, addr):
        raise OSError(self.message)


def _handle(
    proxy, keys, epochs, migration, sock, packet, *, clock=lambda: 0.0,
    expected_ping_seq=None, addr=REMOTE_ADDR, remote_addr=REMOTE_ADDR,
):
    return proxy._try_handle_path_message(
        packet, addr, remote_addr, epochs, migration, STATION_ID, sock,
        clock, expected_ping_seq,
    )


def _establish(
    proxy, keys, epochs, migration, sock, *, token, generation,
    clock=lambda: 0.0, expected_ping_seq=None, epoch_generation=0, key=None,
):
    """Drive one authenticated PATH_CHALLENGE through `_try_handle_path_message`
    and assert it was answered with exactly one matching PATH_RESPONSE."""
    packet = _challenge_packet(
        proxy, keys, token=token, generation=generation,
        epoch_generation=epoch_generation, key=key,
    )
    before = len(sock.sent)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, packet, clock=clock,
        expected_ping_seq=expected_ping_seq,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    assert len(sock.sent) == before + 1
    addr, message = sock.sent[-1]
    assert message["type"] == udpsec_protocol.PATH_RESPONSE_TYPE
    assert message["path_generation"] == generation
    assert base64.b64decode(message["challenge_token"]) == token
    return message


# --------------------------------------------------------------------------
# A. PATH_CHALLENGE classification: new / retry / duplicate / stale-reject
# --------------------------------------------------------------------------


def test_new_challenge_establishes_proof_and_captures_ping_context(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0, expected_ping_seq=17,
    )

    # The established proof captured ping #17 -- proven behaviorally: an
    # ACK matching this proof while #17 is still outstanding clears it.
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack, clock=lambda: 1.0,
        expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 17


def test_challenge_alone_gives_no_liveness_credit(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)

    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _challenge_packet(proxy, keys, token=token, generation=1),
        expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None


def test_stale_older_generation_challenge_gets_no_response_and_no_state(
    proxy, keys
):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token_hi = os.urandom(32)
    token_lo = os.urandom(32)

    _establish(
        proxy, keys, epochs, migration, sock, token=token_hi, generation=5,
    )
    before = len(sock.sent)

    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _challenge_packet(proxy, keys, token=token_lo, generation=3),
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    # A strictly older generation gets no response at all -- fail closed.
    assert len(sock.sent) == before

    # The still-live proof for generation 5 is unaffected.
    ack = _ack_packet(proxy, keys, token=token_hi, generation=5)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack, expected_ping_seq=None,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED


def test_conflicting_token_for_known_generation_fails_closed(proxy, keys):
    """Section: 'If the same generation arrives with conflicting token
    contents, fail closed for migration/liveness authority; do not replace
    a valid existing proof with inconsistent state.'"""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    other_token = os.urandom(32)

    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)
    before = len(sock.sent)

    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _challenge_packet(proxy, keys, token=other_token, generation=1),
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    assert len(sock.sent) == before  # no response sent for the conflict

    # The original, valid proof is untouched and still works.
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, _ = _handle(proxy, keys, epochs, migration, sock, ack)
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED


def test_newer_generation_replaces_previous_pending_proof(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token1 = os.urandom(32)
    token2 = os.urandom(32)

    _establish(
        proxy, keys, epochs, migration, sock, token=token1, generation=1,
        clock=lambda: 0.0, expected_ping_seq=17,
    )
    _establish(
        proxy, keys, epochs, migration, sock, token=token2, generation=2,
        clock=lambda: 2.0, expected_ping_seq=18,
    )

    # The OLD proof (generation 1) lost authority: its own ACK no longer
    # matches anything, even though it would still be within its own
    # original deadline.
    old_ack = _ack_packet(proxy, keys, token=token1, generation=1)
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, old_ack, clock=lambda: 3.0,
        expected_ping_seq=18,
    )
    assert result == proxy.SERVER_PACKET_IGNORED

    # The NEW proof (generation 2) works and captured the NEW ping context.
    new_ack = _ack_packet(proxy, keys, token=token2, generation=2)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, new_ack, clock=lambda: 3.0,
        expected_ping_seq=18,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 18


def test_duplicate_challenge_may_be_answered_again_but_never_rearms(
    proxy, keys
):
    """Requirement 6/7: a duplicate of an already-established incarnation
    may receive another PATH_RESPONSE, but must not move the original
    deadline or recapture the ping sequence, and must not revive an
    already-expired proof."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    ttl = proxy.CLIENT_PATH_PROOF_TTL_SECONDS

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0, expected_ping_seq=17,
    )

    # A duplicate delivery of the SAME challenge, with a DIFFERENT ping
    # outstanding now, still gets answered...
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: ttl / 2, expected_ping_seq=99,
    )

    # ...but the ORIGINAL ping context (17, not 99) is what an ACK still
    # arriving before the ORIGINAL deadline supersedes:
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack,
        clock=lambda: ttl - 0.1, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 17


def test_duplicate_challenge_does_not_extend_deadline(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    ttl = proxy.CLIENT_PATH_PROOF_TTL_SECONDS

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0, expected_ping_seq=17,
    )
    # A duplicate arrives well after the original send, at t=ttl/2.
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: ttl / 2, expected_ping_seq=17,
    )

    # If the duplicate had (incorrectly) rearmed the deadline from t=ttl/2,
    # an ACK at t=ttl + 0.1 would still be accepted. It must NOT be: the
    # ORIGINAL deadline (anchored at t=0) governs.
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, ack,
        clock=lambda: ttl + 0.1, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED


# --------------------------------------------------------------------------
# B. PATH_ACK admission: exact match, one-shot consumption, expiry
# --------------------------------------------------------------------------


def test_unseen_generation_ack_without_prior_challenge_has_no_effect(
    proxy, keys
):
    """Requirement 3: a greater-generation ACK the client never saw a
    matching challenge for must not be treated as fresh proof."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)

    ack = _ack_packet(proxy, keys, token=os.urandom(32), generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack, expected_ping_seq=None,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None


def test_wrong_token_ack_has_no_effect(proxy, keys):
    """Requirement 2: right generation, authenticated, wrong token."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    wrong_token = os.urandom(32)

    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)

    ack = _ack_packet(proxy, keys, token=wrong_token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None

    # The real proof is still live and usable.
    good_ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, _ = _handle(proxy, keys, epochs, migration, sock, good_ack)
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED


def test_ack_remains_live_just_before_proof_deadline(proxy, keys):
    """Proof authority remains usable strictly before its fixed deadline."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    ttl = proxy.CLIENT_PATH_PROOF_TTL_SECONDS

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0,
    )

    ack = _ack_packet(proxy, keys, token=token, generation=1)
    # Just before the deadline: still live.
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, ack, clock=lambda: ttl - 0.001,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED


def test_exact_expiry_boundary_rejects_at_equality_second_proof(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    ttl = proxy.CLIENT_PATH_PROOF_TTL_SECONDS

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0,
    )
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    # Exactly at the deadline: expired, must reject.
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, ack, clock=lambda: ttl,
    )
    assert result == proxy.SERVER_PACKET_IGNORED


def test_delayed_first_delivery_of_expired_ack_does_not_clear_a_later_ping(
    proxy, keys
):
    """F1 -- the exact audited bug: a valid PATH_ACK delivered for the
    FIRST time only after its own proof has expired must not reach into
    the future and supersede a later, unrelated outstanding ping."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    ttl = proxy.CLIENT_PATH_PROOF_TTL_SECONDS

    # t=1: PATH_CHALLENGE answered while ping #17 was outstanding.
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 1.0, expected_ping_seq=17,
    )

    # The proof's deadline (t=1+ttl) passes with no ACK ever arriving.
    # Time moves on; ping #17 is independently resolved, and a brand new
    # ping #18 later becomes outstanding.
    late_time = 1.0 + ttl + 39.0  # comfortably past expiry
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack,
        clock=lambda: late_time, expected_ping_seq=18,
    )

    assert result == proxy.SERVER_PACKET_IGNORED, (
        "a PATH_ACK delivered for the first time after its proof expired "
        "must not be treated as fresh migration evidence"
    )
    assert ping_seq_to_clear is None


def test_ack_does_not_clear_a_different_later_ping_even_when_fresh(
    proxy, keys
):
    """Requirement 9: even a FRESH (unexpired, matching) ACK must only
    clear the SAME ping context it was established against -- never a
    different ping that happens to be outstanding when the ACK arrives."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0, expected_ping_seq=None,
    )

    # By the time the (still-fresh, still-matching) ACK arrives, a NEW
    # ping #18 has since been sent.
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack,
        clock=lambda: 1.0, expected_ping_seq=18,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear is None


def test_ack_clears_the_same_ping_it_was_captured_against(proxy, keys):
    """Requirement 10."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)

    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0, expected_ping_seq=17,
    )
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack,
        clock=lambda: 1.0, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 17


def test_ack_is_consumed_exactly_once(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)

    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)
    ack = _ack_packet(proxy, keys, token=token, generation=1)

    result, _ = _handle(proxy, keys, epochs, migration, sock, ack)
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED

    # A byte-for-byte replay of the exact same ACK: the proof has already
    # been consumed, so no repeated liveness credit.
    result, ping_seq_to_clear = _handle(proxy, keys, epochs, migration, sock, ack)
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None


def test_path_ack_from_wrong_source_address_is_ignored(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)

    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, ack, addr=OTHER_ADDR,
    )
    assert result is None


def test_path_ack_with_wrong_station_id_is_ignored(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)

    ack = _ack_packet(
        proxy, keys, token=token, generation=1, station_id="someone_else",
    )
    result, _ = _handle(proxy, keys, epochs, migration, sock, ack)
    assert result is None


def test_path_ack_forged_under_wrong_key_fails_authentication(proxy, keys):
    """Section 9: a packet must not influence liveness merely because it
    syntactically resembles PATH_ACK -- it must pass AEAD authentication
    under the session's real server->client key."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)

    forged_key = AESGCM.generate_key(bit_length=256)
    ack = _ack_packet(proxy, keys, token=token, generation=1, key=forged_key)
    result, _ = _handle(proxy, keys, epochs, migration, sock, ack)
    assert result is None


def test_malformed_path_message_is_ignored(proxy, keys):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    # Well-formed DATA framing, but the decrypted JSON is not a valid
    # canonical PATH_ACK (missing required members).
    bogus = {"type": udpsec_protocol.PATH_ACK_TYPE, "source_id": STATION_ID}
    packet = proxy.encrypt_secure_json_message(
        bogus, keys["s2c"], keys["locator"], 0
    )
    result, _ = _handle(proxy, keys, epochs, migration, sock, packet)
    assert result is None


def test_late_pong_for_superseded_ping_has_no_effect_after_migration(
    proxy, keys
):
    """Requirement 10.D: once a PATH_ACK has superseded the outstanding
    ping (expected_ping_seq cleared), a late/stale PONG for that same old
    sequence must not resurrect anything -- `expected_ping_seq` is already
    None, so `handle_server_packet` cannot match it (pre-existing,
    unmodified pong machinery)."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        expected_ping_seq=17,
    )
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 17

    # forward_loop would now have set expected_ping_seq = None
    late_pong = udpsec_protocol.build_pong_message(STATION_ID, 17, 1)
    late_packet = proxy.encrypt_secure_json_message(
        late_pong, keys["s2c"], keys["locator"], 0
    )
    pong_result = proxy.handle_server_packet(
        late_packet, REMOTE_ADDR, REMOTE_ADDR, keys["s2c"], keys["locator"],
        STATION_ID, None, 0,
    )
    assert pong_result == proxy.SERVER_PACKET_IGNORED


# --------------------------------------------------------------------------
# C. Epoch interaction: real retiring epoch, and migration across a refresh
# --------------------------------------------------------------------------


def test_path_ack_under_pending_epoch_is_ignored(proxy, keys):
    """A path migration never promotes/discards/rekeys the CryptoEpoch:
    PATH_ACK is only ever valid under the session's exact CURRENT epoch,
    never a pending (in-flight refresh) one."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)

    pending_c2s = AESGCM.generate_key(bit_length=256)
    pending_s2c = AESGCM.generate_key(bit_length=256)
    epochs.set_pending(1, pending_c2s, pending_s2c)

    ack = _ack_packet(
        proxy, keys, token=token, generation=1, key=pending_s2c,
        epoch_generation=1,
    )
    result, _ = _handle(proxy, keys, epochs, migration, sock, ack)
    assert result is None


def test_path_ack_under_a_real_retiring_epoch_is_ignored(proxy, keys):
    """Requirement 13: a REAL retiring epoch (a completed refresh commit,
    not merely a pending one) must not be able to open or prove a
    migration -- exact mirror of the server-side rule that a retiring
    epoch's traffic can never do so."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    old_s2c = keys["s2c"]

    # Establish a proof, then actually COMMIT a refresh: epoch 0 becomes
    # retiring (still briefly decryptable), epoch 1 becomes current.
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 0.0,
    )
    new_c2s = AESGCM.generate_key(bit_length=256)
    new_s2c = AESGCM.generate_key(bit_length=256)
    epochs.set_pending(1, new_c2s, new_s2c)
    assert epochs.commit_pending(1.0) is True
    assert epochs.generation == 1

    # An ACK encrypted under the now-retiring epoch 0 key: still within
    # the retiring window, still decryptable, but must be rejected because
    # it does not authenticate under the exact CURRENT epoch (1).
    ack = _ack_packet(
        proxy, keys, token=token, generation=1, key=old_s2c,
        epoch_generation=0,
    )
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, ack, clock=lambda: 2.0,
    )
    assert result is None


def test_old_epoch_bound_proof_cannot_authorize_ack_after_refresh_commit(
    proxy, keys
):
    """Requirement 14: the proof itself remembers the exact CryptoEpoch
    authority its PATH_RESPONSE was sent under. Even in the (server-
    disallowed, but defended-in-depth client-side) hypothetical where an
    ACK for the SAME generation/token is freshly authenticated under the
    NEW current epoch after a refresh commits, the client's own proof-level
    epoch check must still refuse it -- the proof was established under
    the OLD epoch authority, not the new one. The generation watermark
    itself survives the refresh, so a genuinely NEW challenge afterward
    still works normally."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys, epochs)
    token1 = os.urandom(32)

    _establish(
        proxy, keys, epochs, migration, sock, token=token1, generation=1,
        clock=lambda: 0.0, expected_ping_seq=17,
    )

    new_c2s = AESGCM.generate_key(bit_length=256)
    new_s2c = AESGCM.generate_key(bit_length=256)
    epochs.set_pending(1, new_c2s, new_s2c)
    assert epochs.commit_pending(1.0) is True
    assert epochs.generation == 1

    # Hypothetical ACK: same generation/token as the OLD proof, but
    # authenticated under the NEW current epoch -- passes the dispatcher's
    # outer current-epoch gate, so only the proof's own epoch-authority
    # check can catch it.
    hypothetical_ack = _ack_packet(
        proxy, keys, token=token1, generation=1, key=new_s2c,
        epoch_generation=1,
    )
    result, _ = _handle(
        proxy, keys, epochs, migration, sock, hypothetical_ack,
        clock=lambda: 2.0, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED, (
        "a proof established under the OLD epoch must not authorize an "
        "ACK claiming the NEW epoch's authority"
    )

    # The watermark survives the refresh: a genuinely new, strictly
    # greater generation still opens a fresh proof normally. The challenge
    # itself must now authenticate under the NEW current epoch.
    token2 = os.urandom(32)
    _establish(
        proxy, keys, epochs, migration, sock, token=token2, generation=2,
        clock=lambda: 2.0, expected_ping_seq=18, key=new_s2c,
        epoch_generation=1,
    )
    new_ack = _ack_packet(
        proxy, keys, token=token2, generation=2, key=new_s2c,
        epoch_generation=1,
    )
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, new_ack, clock=lambda: 2.5,
        expected_ping_seq=18,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 18


# --------------------------------------------------------------------------
# D. F3 -- PATH_RESPONSE send-failure logging must be ASCII-safe
# --------------------------------------------------------------------------


class _StrictEncodingStream:
    """Reject text that cannot be written to the selected console encoding."""

    def __init__(self, encoding):
        self.encoding = encoding
        self.written = []

    def write(self, s):
        s.encode(self.encoding)
        self.written.append(s)
        return len(s)

    def flush(self):
        pass


@pytest.mark.parametrize(
    "encoding,error_text",
    [
        ("ascii", "network is unreachable"),
        ("ascii", "Няма маршрут до мрежата"),
        ("cp1251", "网络不可达"),
    ],
)
def test_path_response_send_failure_is_ascii_safe_and_leaves_no_proof(
    proxy, keys, monkeypatch, encoding, error_text
):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _FailingSock(error_text)
    token = os.urandom(32)

    fake_stdout = _StrictEncodingStream(encoding)
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    try:
        result, ping_seq_to_clear = _handle(
            proxy, keys, epochs, migration, sock,
            _challenge_packet(proxy, keys, token=token, generation=1),
            expected_ping_seq=17,
        )
    finally:
        monkeypatch.undo()

    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    logged = "".join(fake_stdout.written)
    assert "Path response send error" in logged
    assert error_text.encode("ascii", "backslashreplace").decode("ascii") in logged
    logged.encode("ascii")  # re-assert: no UnicodeEncodeError was swallowed

    # No usable proof was left behind by the failed send.
    real_sock = _RecordingSock(proxy, keys)
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    result, _ = _handle(proxy, keys, epochs, migration, real_sock, ack)
    assert result == proxy.SERVER_PACKET_IGNORED

    # A retry of the SAME challenge (now with a working sock) succeeds and
    # establishes a fresh, usable proof.
    _establish(
        proxy, keys, epochs, migration, real_sock, token=token, generation=1,
        expected_ping_seq=17,
    )
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, real_sock, ack, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 17


# --------------------------------------------------------------------------
# E. End-to-end through the REAL forward_loop()
# --------------------------------------------------------------------------


class _FakeInput:
    def selectable_sockets(self):
        return []

    def poll_interval(self):
        return None

    def read_ready(self, _sock):
        return []

    def read_pending(self):
        return []

    def start(self):
        pass

    def close(self):
        pass


class _StopLoop(BaseException):
    pass


def test_e2e_lost_pong_migration_supersedes_stale_ping_without_grace(
    proxy, keys, monkeypatch
):
    """The canonical Major Prompt 5 scenario, end-to-end through the REAL
    `forward_loop()`:

        PING #1 -> A
        NAT A -> B; PONG #1 is lost
        server issues PATH_CHALLENGE(B) instead
        client answers with PATH_RESPONSE(B)
        server commits and sends PATH_ACK(B)

    Required result: the client does NOT proactively rekey despite the
    unanswered ping #1, and the NEXT ordinary keepalive PING #2 fires on the
    session's ORIGINAL un-extended schedule (last_ping_at + keepalive_
    interval) -- no migration-granted grace period.
    """
    locator = keys["locator"]
    c2s, s2c = keys["c2s"], keys["s2c"]
    server = {"path_generation": 0, "pings": [], "token": None, "ping_times": {}}

    class FakeSock:
        def __init__(self):
            self.inbox = []

        def sendto(self, data, addr):
            assert addr == REMOTE_ADDR
            l, _sel, nonce, ct = udpsec_protocol.parse_data_packet(data)
            plaintext = AESGCM(c2s).decrypt(nonce, ct, proxy.build_data_aad(l, 0))
            message = json.loads(plaintext)
            kind = message.get("type")
            if kind == "ping":
                server["pings"].append(message["seq"])
                if message["seq"] == 1:
                    # Simulate the NAT rebind: PONG #1 is lost and the
                    # server instead observes this traffic from a new path
                    # and challenges it.
                    server["path_generation"] = 1
                    server["token"] = os.urandom(32)
                    challenge = udpsec_protocol.build_path_challenge_message(
                        station_id=STATION_ID,
                        challenge_token=server["token"],
                        path_generation=server["path_generation"],
                        timestamp=1,
                    )
                    self.inbox.append(
                        proxy.encrypt_secure_json_message(
                            challenge, s2c, locator, 0
                        )
                    )
                else:
                    pong = udpsec_protocol.build_pong_message(
                        STATION_ID, message["seq"], 1
                    )
                    self.inbox.append(
                        proxy.encrypt_secure_json_message(pong, s2c, locator, 0)
                    )
            elif kind == udpsec_protocol.PATH_RESPONSE_TYPE:
                assert message["path_generation"] == server["path_generation"]
                assert (
                    base64.b64decode(message["challenge_token"])
                    == server["token"]
                )
                ack = udpsec_protocol.build_path_ack_message(
                    station_id=STATION_ID,
                    challenge_token=server["token"],
                    path_generation=server["path_generation"],
                    timestamp=1,
                )
                self.inbox.append(
                    proxy.encrypt_secure_json_message(ack, s2c, locator, 0)
                )

        def recvfrom(self, _n):
            if self.inbox:
                return self.inbox.pop(0), REMOTE_ADDR
            raise BlockingIOError()

    clock = {"t": 0.0}
    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    def fake_select(r, w, x, t):
        clock["t"] += 0.25
        if "ping2_at" in server:
            raise _StopLoop()
        return ([s for s in r if getattr(s, "inbox", None)], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    config = {
        "station_id": STATION_ID,
        "keepalive_interval": 5,
        "peer_timeout": 100000,
        "session_refresh_interval": 0,
        "reconnect_delay": 1,
    }
    confirmed = proxy.ConfirmedUdpsecSession(
        session_locator=locator,
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=c2s, server_to_client_key=s2c
        ),
    )
    sock = FakeSock()
    stats = proxy.ForwardingStats()

    original_sendto = FakeSock.sendto

    def sendto_and_track(self, data, addr):
        pings_before = len(server["pings"])
        original_sendto(self, data, addr)
        if len(server["pings"]) > pings_before:
            seq = server["pings"][-1]
            server["ping_times"].setdefault(seq, clock["t"])
            if seq == 2:
                server["ping2_at"] = clock["t"]

    monkeypatch.setattr(FakeSock, "sendto", sendto_and_track)

    reason = "not_started"
    try:
        reason = proxy.forward_loop(
            _FakeInput(), sock, config, confirmed, REMOTE_ADDR, None, stats,
        )
    except _StopLoop:
        reason = "stopped_by_test"

    assert reason == "stopped_by_test", (
        f"forward_loop exited early with reason={reason!r} instead of "
        "sending ping #2 -- the migration must have failed to supersede "
        "the stale outstanding ping"
    )
    assert server["pings"] == [1, 2]
    ping1_at = server["ping_times"][1]
    ping2_at = server["ping_times"][2]
    gap = ping2_at - ping1_at
    # The challenge/response/ack round trip completes within ~3 ticks
    # (0.75s) of ping #1 -- well inside the 5s keepalive_interval. The gap
    # to ping #2 must equal the ORIGINAL keepalive_interval (within one
    # 0.25s tick of slop for the deadline-check granularity), proving
    # `last_ping_at` was never reset and no migration grace period was
    # granted.
    assert 5.0 <= gap <= 5.25, (
        f"keepalive gap between ping #1 and #2 was {gap!r}s; expected "
        "~5.0s (the configured keepalive_interval, unmodified by the "
        "intervening path migration)"
    )
    assert stats.messages == 0


def test_e2e_path_migration_does_not_postpone_planned_refresh(
    proxy, keys, monkeypatch
):
    """Requirement 15/section 6: a successful migration completing shortly
    after the session starts must NOT push the planned
    `session_refresh_interval` deadline out. No refresh keys are supplied,
    so a due planned refresh falls back to `SESSION_END_PLANNED_REFRESH` --
    the simplest observable proxy for `session_started_at`."""
    locator = keys["locator"]
    c2s, s2c = keys["c2s"], keys["s2c"]
    token = os.urandom(32)
    challenge = udpsec_protocol.build_path_challenge_message(
        station_id=STATION_ID,
        challenge_token=token,
        path_generation=1,
        timestamp=1,
    )

    class FakeSock:
        def __init__(self):
            self.inbox = [
                proxy.encrypt_secure_json_message(challenge, s2c, locator, 0)
            ]

        def sendto(self, data, addr):
            l, _sel, nonce, ct = udpsec_protocol.parse_data_packet(data)
            plaintext = AESGCM(c2s).decrypt(nonce, ct, proxy.build_data_aad(l, 0))
            message = json.loads(plaintext)
            if message.get("type") == udpsec_protocol.PATH_RESPONSE_TYPE:
                ack = udpsec_protocol.build_path_ack_message(
                    station_id=STATION_ID,
                    challenge_token=token,
                    path_generation=1,
                    timestamp=1,
                )
                self.inbox.append(
                    proxy.encrypt_secure_json_message(ack, s2c, locator, 0)
                )

        def recvfrom(self, _n):
            if self.inbox:
                return self.inbox.pop(0), REMOTE_ADDR
            raise BlockingIOError()

    clock = {"t": 0.0}
    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    def fake_select(r, w, x, t):
        clock["t"] += 0.25
        return ([s for s in r if getattr(s, "inbox", None)], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    config = {
        "station_id": STATION_ID,
        "keepalive_interval": 100000,
        "peer_timeout": 100000,
        "session_refresh_interval": 5,
        "reconnect_delay": 1,
    }
    confirmed = proxy.ConfirmedUdpsecSession(
        session_locator=locator,
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=c2s, server_to_client_key=s2c
        ),
    )
    sock = FakeSock()
    stats = proxy.ForwardingStats()

    reason = proxy.forward_loop(
        _FakeInput(), sock, config, confirmed, REMOTE_ADDR, None, stats,
    )

    assert reason == proxy.SESSION_END_PLANNED_REFRESH
    # The migration (challenge -> response -> ack) completes within the
    # first couple of ticks (~0.5-0.75s); a buggy reanchor of
    # `session_started_at` to the migration-completion time would push the
    # refresh deadline out to roughly migration_time + 5 (>= 5.25s+). The
    # un-reanchored deadline is exactly 5.0s from session start.
    assert 4.75 <= clock["t"] <= 5.25, (
        f"planned refresh fired at t={clock['t']!r}; expected ~5.0s from "
        "session start, not delayed by the intervening path migration"
    )


def test_e2e_migration_reanchors_peer_timeout_but_not_keepalive_or_refresh(
    proxy, keys, monkeypatch
):
    """Requirement 11: a valid, timely PATH_ACK correctly advances
    `last_authenticated_peer` and therefore reanchors `peer_timeout`
    forward from the moment it is accepted -- this is the intended effect,
    not a bug. Configured with a short `peer_timeout` that the ORIGINAL
    session start would have missed, but that the migration's liveness
    credit lets the session survive past."""
    locator = keys["locator"]
    c2s, s2c = keys["c2s"], keys["s2c"]
    token = os.urandom(32)
    challenge = udpsec_protocol.build_path_challenge_message(
        station_id=STATION_ID,
        challenge_token=token,
        path_generation=1,
        timestamp=1,
    )

    class FakeSock:
        def __init__(self):
            self.inbox = [
                proxy.encrypt_secure_json_message(challenge, s2c, locator, 0)
            ]

        def sendto(self, data, addr):
            l, _sel, nonce, ct = udpsec_protocol.parse_data_packet(data)
            plaintext = AESGCM(c2s).decrypt(nonce, ct, proxy.build_data_aad(l, 0))
            message = json.loads(plaintext)
            if message.get("type") == udpsec_protocol.PATH_RESPONSE_TYPE:
                ack = udpsec_protocol.build_path_ack_message(
                    station_id=STATION_ID,
                    challenge_token=token,
                    path_generation=1,
                    timestamp=1,
                )
                self.inbox.append(
                    proxy.encrypt_secure_json_message(ack, s2c, locator, 0)
                )

        def recvfrom(self, _n):
            if self.inbox:
                return self.inbox.pop(0), REMOTE_ADDR
            raise BlockingIOError()

    clock = {"t": 0.0}
    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    def fake_select(r, w, x, t):
        clock["t"] += 0.25
        return ([s for s in r if getattr(s, "inbox", None)], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    # peer_timeout=6: if the migration ack did NOT reanchor
    # last_authenticated_peer, the session would die at t~=6.0. The
    # migration completes at roughly t~=0.5-0.75, so a correctly reanchored
    # deadline lands at roughly 6.5-6.75, comfortably past 6.0.
    config = {
        "station_id": STATION_ID,
        "keepalive_interval": 100000,
        "peer_timeout": 6,
        "session_refresh_interval": 0,
        "reconnect_delay": 1,
    }
    confirmed = proxy.ConfirmedUdpsecSession(
        session_locator=locator,
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=c2s, server_to_client_key=s2c
        ),
    )
    sock = FakeSock()
    stats = proxy.ForwardingStats()

    reason = proxy.forward_loop(
        _FakeInput(), sock, config, confirmed, REMOTE_ADDR, None, stats,
    )

    assert reason == proxy.SESSION_END_PEER_TIMEOUT
    # ACK admission is at t=0.5. Peer timeout must be exactly six
    # seconds later: arbitrary extra liveness credit must also fail.
    assert clock["t"] == pytest.approx(6.5)


def test_newer_challenge_invalidates_old_proof_before_failed_send_and_retry(
    proxy, keys
):
    """C1: observing g2 supersedes g1 even if responding to g2 fails."""
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token1, token2 = os.urandom(32), os.urandom(32)
    _establish(
        proxy, keys, epochs, migration, sock, token=token1, generation=1,
        clock=lambda: 1.0, expected_ping_seq=17,
    )

    class SupersedingSendFailure:
        def sendto(self, data, addr):
            # Supersession must precede the send attempt, not depend on
            # successful delivery or on reaching the exception handler.
            assert migration._watermark_generation == 2
            assert migration._watermark_token == token2
            assert migration._pending is None
            raise OSError("network is unreachable")

    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, SupersedingSendFailure(),
        _challenge_packet(proxy, keys, token=token2, generation=2),
        clock=lambda: 2.0, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    assert migration._pending is None

    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _ack_packet(proxy, keys, token=token1, generation=1),
        clock=lambda: 3.0, expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None

    _establish(
        proxy, keys, epochs, migration, sock, token=token2, generation=2,
        clock=lambda: 4.0, expected_ping_seq=18,
    )
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _ack_packet(proxy, keys, token=token2, generation=2),
        clock=lambda: 5.0, expected_ping_seq=18,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 18


@pytest.mark.parametrize("watermark_change", ["generation", "token"])
def test_ack_defends_against_proof_inconsistent_with_watermark(
    proxy, keys, watermark_change
):
    """The additional ACK guards also reject accidentally retained state.

    Normal challenge handling no longer creates this inconsistent state;
    deliberately corrupt only the watermark to exercise each guard alone.
    """
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(proxy, keys, epochs, migration, sock, token=token, generation=1)
    if watermark_change == "generation":
        migration._watermark_generation = 2
    else:
        migration._watermark_token = os.urandom(32)
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _ack_packet(proxy, keys, token=token, generation=1),
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    assert migration._pending.consumed is False


@pytest.mark.parametrize("wrong_generation", [1, 3])
def test_ack_with_same_token_but_wrong_generation_does_not_consume_proof(
    proxy, keys, wrong_generation
):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=2,
        expected_ping_seq=17,
    )
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _ack_packet(proxy, keys, token=token, generation=wrong_generation),
        expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _ack_packet(proxy, keys, token=token, generation=2),
        expected_ping_seq=17,
    )
    assert result == proxy.SERVER_PACKET_PATH_MIGRATED
    assert ping_seq_to_clear == 17


@pytest.mark.parametrize("original_state", ["expired", "consumed"])
def test_duplicate_challenge_cannot_revive_expired_or_consumed_proof(
    proxy, keys, original_state
):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    sock = _RecordingSock(proxy, keys)
    token = os.urandom(32)
    ttl = proxy.CLIENT_PATH_PROOF_TTL_SECONDS
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: 1.0, expected_ping_seq=17,
    )
    ack = _ack_packet(proxy, keys, token=token, generation=1)
    if original_state == "consumed":
        result, _ = _handle(
            proxy, keys, epochs, migration, sock, ack, clock=lambda: 2.0,
            expected_ping_seq=17,
        )
        assert result == proxy.SERVER_PACKET_PATH_MIGRATED
        replay_at = 3.0
    else:
        replay_at = 1.0 + ttl

    # A response may be repeated, but it cannot start another lifetime or
    # turn a consumed proof into fresh evidence for ping #18.
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: replay_at, expected_ping_seq=18,
    )
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock, ack,
        clock=lambda: replay_at + 0.1, expected_ping_seq=18,
    )
    assert result == proxy.SERVER_PACKET_IGNORED
    assert ping_seq_to_clear is None


@pytest.mark.parametrize("at_deadline", [False, True])
def test_proof_deadline_starts_after_delayed_successful_response_send(
    proxy, keys, at_deadline
):
    epochs = _epochs(proxy, keys)
    migration = proxy._ClientPathMigration()
    clock = {"t": 1.0}

    class DelayedSock(_RecordingSock):
        def sendto(self, data, addr):
            super().sendto(data, addr)
            clock["t"] = 4.0

    sock = DelayedSock(proxy, keys)
    token = os.urandom(32)
    _establish(
        proxy, keys, epochs, migration, sock, token=token, generation=1,
        clock=lambda: clock["t"], expected_ping_seq=17,
    )
    deadline = 4.0 + proxy.CLIENT_PATH_PROOF_TTL_SECONDS
    result, ping_seq_to_clear = _handle(
        proxy, keys, epochs, migration, sock,
        _ack_packet(proxy, keys, token=token, generation=1),
        clock=lambda: deadline if at_deadline else deadline - 0.001,
        expected_ping_seq=17,
    )
    if at_deadline:
        assert result == proxy.SERVER_PACKET_IGNORED
        assert ping_seq_to_clear is None
    else:
        assert result == proxy.SERVER_PACKET_PATH_MIGRATED
        assert ping_seq_to_clear == 17


def _run_scripted_path_loop(
    proxy, keys, monkeypatch, events, *, config_overrides=None,
    fail_response_generation=None, station_private_key=None,
    server_identity_public_key=None, on_send=None, clock=None,
):
    """Drive real encrypted packets and deadlines through forward_loop.

    Each event is (poll_ready_at, recv_finishes_at, packet). Separating the
    two times exercises processing across a deadline without sleeping.

    ``station_private_key``/``server_identity_public_key`` opt into the REAL
    in-session epoch-refresh machinery running alongside migration (refresh
    stays disabled, as before, when both are left `None`). ``on_send``, when
    given, is called as ``on_send(message, clock)`` for every outbound
    datagram after it is decrypted and recorded -- e.g. to simulate a send
    itself consuming wall-clock time by mutating ``clock["t"]``. ``clock``,
    when given, is used (and mutated in place) as the live monotonic-clock
    dict instead of a private one, so a caller can splice its own hooks
    (e.g. a patched `print`) into the SAME clock the loop's `time.monotonic`
    reads -- it must already hold `{"t": 0.0}` or equivalent.
    """
    if clock is None:
        clock = {"t": 0.0}
    pending = list(events)
    sent = []
    received = []

    class ScriptedSock:
        def sendto(self, data, addr):
            assert addr == REMOTE_ADDR
            locator, _selector, nonce, ciphertext = (
                udpsec_protocol.parse_data_packet(data)
            )
            plaintext = AESGCM(keys["c2s"]).decrypt(
                nonce, ciphertext, proxy.build_data_aad(locator, 0)
            )
            message = json.loads(plaintext)
            sent.append((clock["t"], message))
            if (
                message.get("type") == udpsec_protocol.PATH_RESPONSE_TYPE
                and message["path_generation"] == fail_response_generation
            ):
                raise OSError("network is unreachable")
            if on_send is not None:
                on_send(message, clock)

        def recvfrom(self, _n):
            ready_at, finished_at, packet = pending.pop(0)
            assert clock["t"] >= ready_at
            clock["t"] = max(clock["t"], finished_at)
            received.append(packet)
            return packet, REMOTE_ADDR

    sock = ScriptedSock()
    poll_count = 0

    def fake_select(readable, writable, exceptional, timeout):
        nonlocal poll_count
        poll_count += 1
        assert poll_count < 1000, "forward_loop failed to reach its deadline"
        wake_at = clock["t"] + timeout
        if pending:
            wake_at = min(wake_at, pending[0][0])
        clock["t"] = max(clock["t"], wake_at)
        ready = [sock] if pending and pending[0][0] <= clock["t"] else []
        return (ready, [], [])

    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)
    monkeypatch.setattr(proxy.select, "select", fake_select)
    config = {
        "station_id": STATION_ID,
        "keepalive_interval": 5,
        "peer_timeout": 100,
        "session_refresh_interval": 0,
        "reconnect_delay": 1,
    }
    config.update(config_overrides or {})
    session = proxy.ConfirmedUdpsecSession(
        session_locator=keys["locator"],
        key_material=proxy.SessionKeyMaterial(
            client_to_server_key=keys["c2s"], server_to_client_key=keys["s2c"]
        ),
    )
    reason = proxy.forward_loop(
        _FakeInput(), sock, config, session, REMOTE_ADDR,
        None, proxy.ForwardingStats(),
        station_private_key=station_private_key,
        server_identity_public_key=server_identity_public_key,
    )
    pings = [
        (when, message["seq"])
        for when, message in sent if message["type"] == "ping"
    ]
    return reason, clock["t"], pings, received


def test_e2e_superseded_ack_after_response_send_failure_cannot_delay_recovery(
    proxy, keys, monkeypatch
):
    token1, token2 = os.urandom(32), os.urandom(32)
    events = [
        (6.0, 6.0, _challenge_packet(proxy, keys, token=token1, generation=1)),
        (7.0, 7.0, _challenge_packet(proxy, keys, token=token2, generation=2)),
        (8.0, 8.0, _ack_packet(proxy, keys, token=token1, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events, fail_response_generation=2,
    )
    assert len(received) == 3
    assert pings == [(5.0, 1)]
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert ended_at == pytest.approx(10.0)


def test_e2e_ack_crossing_keepalive_deadline_cannot_erase_new_ping(
    proxy, keys, monkeypatch
):
    token = os.urandom(32)
    events = [
        (1.0, 1.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (4.99, 5.01, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
    )
    assert len(received) == 2
    assert pings == [(5.01, 1)]
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert ended_at == pytest.approx(10.01)


def test_e2e_ack_cannot_clear_ping_after_captured_ping_was_resolved(
    proxy, keys, monkeypatch
):
    token = os.urandom(32)
    pong1 = proxy.encrypt_secure_json_message(
        udpsec_protocol.build_pong_message(STATION_ID, 1, 1),
        keys["s2c"], keys["locator"], 0,
    )
    events = [
        (6.0, 6.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (7.0, 7.0, pong1),
        (11.0, 11.0, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
    )
    assert len(received) == 3
    assert pings == [(5.0, 1), (10.0, 2)]
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY
    assert ended_at == pytest.approx(15.0)


def test_e2e_matched_ack_without_ping_clear_authority_still_reanchors_liveness(
    proxy, keys, monkeypatch
):
    token = os.urandom(32)
    events = [
        (1.0, 1.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (2.0, 2.0, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
        config_overrides={"keepalive_interval": 100, "peer_timeout": 6},
    )
    assert len(received) == 2
    assert pings == []
    assert reason == proxy.SESSION_END_PEER_TIMEOUT
    assert ended_at == pytest.approx(8.0)


@pytest.mark.parametrize(
    "config_overrides,challenge_at,deadline,expected_reason",
    [
        (
            {"keepalive_interval": 100, "peer_timeout": 5},
            1.0, 5.0, "peer_timeout",
        ),
        (
            {"keepalive_interval": 100, "session_refresh_interval": 5},
            1.0, 5.0, "planned_refresh",
        ),
        ({}, 6.0, 10.0, "proactive_rekey"),
    ],
)
def test_e2e_terminal_deadline_wins_when_ack_receive_reaches_equality(
    proxy, keys, monkeypatch, config_overrides, challenge_at, deadline,
    expected_reason,
):
    token = os.urandom(32)
    events = [
        (
            challenge_at, challenge_at,
            _challenge_packet(proxy, keys, token=token, generation=1),
        ),
        (
            deadline - 0.01, deadline,
            _ack_packet(proxy, keys, token=token, generation=1),
        ),
    ]
    reason, ended_at, _pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events, config_overrides=config_overrides,
    )
    assert len(received) == 2  # ACK was received before the terminal recheck.
    assert reason == expected_reason
    assert ended_at == pytest.approx(deadline)


# --------------------------------------------------------------------------
# F. MP5 corrective round 3 -- HIGH finding: a matched PATH_ACK's liveness/
# ping-clear effects must be gated by a FRESH terminal-deadline admission
# taken immediately before those effects, not by the earlier post-receipt
# check alone. `drive_refresh()` and acknowledgement logging sit between the
# two and can themselves consume enough time for a terminal deadline to
# become due; that deadline must still win.
# --------------------------------------------------------------------------


def _station_keypair():
    """A throwaway station identity keypair for the REAL in-session
    epoch-refresh machinery. No REFRESH_REPLY is ever scripted back, so the
    transaction just keeps retransmitting REFRESH_INIT -- `refresh.tick()`
    performing that retransmission (a real `sock.sendto()` call) is exactly
    the "drive_refresh() does non-trivial work" case the finding is about.
    `server_identity_public_key` is never actually used (no REFRESH_REPLY
    ever arrives to verify), so any real EC public key suffices."""
    station_private_key = ec.generate_private_key(ec.SECP256R1())
    return station_private_key, station_private_key.public_key()


def test_e2e_refresh_retransmit_crossing_proactive_recovery_deadline_wins(
    proxy, keys, monkeypatch
):
    """Regression 1: a matched PATH_ACK is admitted (post-receipt deadline
    check passes) just before the unresolved-ping proactive-recovery
    deadline, but the very next `drive_refresh()` call performs a real
    retransmission whose mocked transport advances the clock past that
    deadline. The proactive-recovery reason must win: the ACK must NOT
    clear ping #1 and must NOT reanchor liveness, so no ping #2 ever
    results from the stale effect."""
    station_private_key, server_identity_public_key = _station_keypair()
    token = os.urandom(32)

    triggered = {"done": False}

    def on_send(message, clock):
        # Only the retransmission that fires while the ACK is being
        # admitted (t=9.0, the third `refresh.tick()` retransmission)
        # should advance the clock -- the three earlier REFRESH_INIT sends
        # (t=3, t=5, t=7) must be left alone.
        if (
            not triggered["done"]
            and message.get("type") == udpsec_protocol.REFRESH_INIT_TYPE
            and clock["t"] >= 9.0
        ):
            triggered["done"] = True
            clock["t"] = 10.5  # crosses the t=10.0 recovery deadline

    events = [
        # t=6: PATH_CHALLENGE answered while ping #1 (sent at keepalive
        # deadline t=5) is still outstanding -- captures it into the proof.
        (6.0, 6.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        # t=8.9: matching PATH_ACK becomes readable; decrypt/admission
        # completes at t=9.0, strictly before the t=10.0 recovery deadline.
        (8.9, 9.0, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 1000,
            "session_refresh_interval": 3,
        },
        station_private_key=station_private_key,
        server_identity_public_key=server_identity_public_key,
        on_send=on_send,
    )
    assert triggered["done"], "the scripted refresh retransmission never fired"
    assert len(received) == 2
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY, (
        f"reason={reason!r}; a refresh retransmission crossing the "
        "recovery deadline during ACK admission must still terminate the "
        "session via proactive recovery"
    )
    assert pings == [(5.0, 1)], (
        "no ping #2 may be sent -- the ACK must not have cleared ping #1 "
        "after the recovery deadline came due"
    )
    assert ended_at == pytest.approx(10.5)


def test_e2e_refresh_retransmit_crossing_peer_timeout_wins(proxy, keys, monkeypatch):
    """Regression 2: same shape as regression 1, but the terminal deadline
    crossed by the retransmission's send is `peer_timeout`, not the
    proactive-recovery/keepalive deadline. `peer_timeout` must win and the
    ACK must not reanchor liveness."""
    station_private_key, server_identity_public_key = _station_keypair()
    token = os.urandom(32)

    triggered = {"done": False}

    def on_send(message, clock):
        if (
            not triggered["done"]
            and message.get("type") == udpsec_protocol.REFRESH_INIT_TYPE
            and clock["t"] >= 9.0
        ):
            triggered["done"] = True
            clock["t"] = 10.5  # crosses the t=10.0 peer_timeout deadline

    events = [
        (1.0, 1.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (8.9, 9.0, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
        config_overrides={
            "keepalive_interval": 100000,
            "peer_timeout": 10,
            "session_refresh_interval": 3,
        },
        station_private_key=station_private_key,
        server_identity_public_key=server_identity_public_key,
        on_send=on_send,
    )
    assert triggered["done"], "the scripted refresh retransmission never fired"
    assert len(received) == 2
    assert reason == proxy.SESSION_END_PEER_TIMEOUT, (
        f"reason={reason!r}; a refresh retransmission crossing peer_timeout "
        "during ACK admission must still terminate the session via "
        "peer_timeout, not be masked by a stale ACK reanchor"
    )
    assert pings == []
    assert ended_at == pytest.approx(10.5)


def test_e2e_ack_logging_crossing_proactive_recovery_deadline_wins(
    proxy, keys, monkeypatch
):
    """Regression 3: refresh disabled entirely (no station keys, so
    `drive_refresh()` is always a no-op) -- the acknowledgement *logging*
    itself is the thing that consumes enough (mocked) time to cross the
    proactive-recovery deadline between the post-receipt check and the
    final admission. The deadline must still win."""
    token = os.urandom(32)
    clock = {"t": 0.0}  # the SAME dict the loop's `time.monotonic` reads

    ACK_MESSAGE = "Secure session path migration acknowledged by peer."
    real_print = builtins.print
    triggered = {"done": False}

    def fake_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        if not triggered["done"] and text == ACK_MESSAGE:
            triggered["done"] = True
            clock["t"] = 10.1  # crosses the t=10.0 recovery deadline
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", fake_print)

    events = [
        (6.0, 6.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (9.9, 9.9, _ack_packet(proxy, keys, token=token, generation=1)),
    ]

    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 1000,
        },
        clock=clock,
    )
    assert triggered["done"], "the scripted acknowledgement log line never fired"
    assert len(received) == 2
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY, (
        f"reason={reason!r}; acknowledgement logging crossing the recovery "
        "deadline during ACK admission must still terminate the session "
        "via proactive recovery"
    )
    assert pings == [(5.0, 1)], (
        "no ping #2 may be sent -- the ACK must not have cleared ping #1 "
        "after the recovery deadline came due"
    )
    assert ended_at == pytest.approx(10.1)


def test_e2e_final_admission_exact_equality_rejects_ack_effects(
    proxy, keys, monkeypatch
):
    """Regression 4: the FINAL fresh admission (not the earlier post-receipt
    check) lands at EXACT equality with `peer_timeout`. Exact equality is
    terminal: the deadline must still win, with no ACK liveness/ping
    mutation."""
    station_private_key, server_identity_public_key = _station_keypair()
    token = os.urandom(32)

    triggered = {"done": False}

    def on_send(message, clock):
        if (
            not triggered["done"]
            and message.get("type") == udpsec_protocol.REFRESH_INIT_TYPE
            and clock["t"] >= 9.0
        ):
            triggered["done"] = True
            clock["t"] = 10.0  # lands EXACTLY on the peer_timeout deadline

    events = [
        (1.0, 1.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (8.9, 9.0, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
        config_overrides={
            "keepalive_interval": 100000,
            "peer_timeout": 10,
            "session_refresh_interval": 3,
        },
        station_private_key=station_private_key,
        server_identity_public_key=server_identity_public_key,
        on_send=on_send,
    )
    assert triggered["done"], "the scripted refresh retransmission never fired"
    assert reason == proxy.SESSION_END_PEER_TIMEOUT, (
        f"reason={reason!r}; now == peer_timeout deadline at the final "
        "admission must still be treated as due (equality is terminal)"
    )
    assert pings == []
    assert ended_at == pytest.approx(10.0)


def test_e2e_control_ack_still_clears_ping_and_reanchors_liveness(
    proxy, keys, monkeypatch
):
    """Regression 5 (control): when nothing crosses a deadline between the
    post-receipt check and the final admission, a matched PATH_ACK still
    works exactly as before -- it clears the concrete matching ping AND
    reanchors liveness to that admission's fresh sample.

    `peer_timeout=12` is chosen so the two effects are jointly observable
    in the final session-end reason: if the ping were NOT cleared, the
    session would end via proactive recovery at t=10 (ping #2 never sent).
    If liveness were NOT reanchored to the ACK's fresh admission time
    (t=6.5), `peer_timeout` would fire at t=12 -- before the next
    keepalive/proactive-recovery deadline at t=15. Only when BOTH effects
    apply correctly does the session survive to send ping #2 at t=10 and
    then end via proactive recovery at t=15 (peer_timeout reanchored to
    6.5 + 12 = 18.5, well past 15).
    """
    token = os.urandom(32)
    events = [
        (6.0, 6.0, _challenge_packet(proxy, keys, token=token, generation=1)),
        (6.5, 6.5, _ack_packet(proxy, keys, token=token, generation=1)),
    ]
    reason, ended_at, pings, received = _run_scripted_path_loop(
        proxy, keys, monkeypatch, events,
        config_overrides={
            "keepalive_interval": 5,
            "peer_timeout": 12,
        },
    )
    assert len(received) == 2
    assert reason == proxy.SESSION_END_PROACTIVE_REKEY, (
        f"reason={reason!r}; a valid pre-deadline ACK must clear ping #1 "
        "(enabling ping #2) and reanchor peer_timeout forward, so the "
        "session should survive past an unreanchored t=12 peer_timeout "
        "and instead end via the unanswered ping #2's recovery at t=15"
    )
    assert pings == [(5.0, 1), (10.0, 2)], (
        "ping #2 must be sent on the normal cadence -- proof the ACK "
        "cleared ping #1"
    )
    assert ended_at == pytest.approx(15.0)
