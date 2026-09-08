import os
import asyncio
import base64
import binascii
import heapq
import itertools
import json
import threading
import time
import weakref
import yaml
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core.endpoint_display import format_endpoint, format_endpoint_tuple
from core.ingress_frame import frame_from_text_payload
from core.network_policy import NetworkPolicy
from core.session_identity_registry import (
    SessionIdentityExhaustedError,
    SessionIdentityRegistry,
)
from core.sockaddr_identity import (
    MalformedSockaddrError,
    normalize_sockaddr,
    sockaddrs_match,
)
from core.source_identity import build_udpsec_source_id
from core.udp_listener import create_udp_listener_socket
from core.udpsec_crypto import (
    DOMAIN_CONTEXT,
    build_client_auth_digest,
    build_server_auth_digest,
    build_session_transcript_hash,
    derive_ephemeral_shared_secret,
    derive_session_key_material,
    generate_ephemeral_private_key,
    parse_ephemeral_public_key,
    serialize_ephemeral_public_key,
    sign_transcript_digest,
    verify_transcript_signature,
)
from core.udpsec_protocol import (
    CLIENT_HELLO_PREFIX,
    DATA_PREFIX,
    SESSION_CLOSE_TYPE,
    SESSION_CONFIRMATION_SEQUENCE,
    SESSION_LOCATOR_BYTES,
    UDPSEC_PROTOCOL_VERSION,
    ServerHello,
    build_data_aad,
    build_data_packet,
    build_pong_message,
    build_session_close_message,
    build_server_hello_packet,
    is_ping_message,
    is_session_close_message,
    parse_client_hello_packet,
    parse_data_packet,
)


SESSION_TTL_SECONDS = 300
SESSION_MAX = 100000
PENDING_SESSION_TTL_SECONDS = 30
PENDING_SESSION_MAX = SESSION_MAX
HANDSHAKE_REPLAY_TTL_SECONDS = 60
HANDSHAKE_REPLAY_MAX = 100000
DATA_NONCE_MAX_PER_SESSION = 100000
SESSION_LOCATOR_GENERATION_ATTEMPTS = 8
# F3: how often `SecureState.run_periodic_maintenance()` (a small,
# independent, opt-in background task -- see that method) runs expiry and
# retirement-purge housekeeping. Not security-relevant on its own (an
# expired owner is already correctly rejected by every lazy per-operation
# check, in `_secure_server_loop` and elsewhere, regardless of when the
# last sweep ran) -- this only bounds how long genuinely idle,
# already-expired state and retired identifier reservations can sit
# before their memory is reclaimed on a listener that receives no
# packets at all.
IDLE_MAINTENANCE_INTERVAL_SECONDS = 30.0

_HANDSHAKE_REPLAY_LABEL = b"HANDSHAKE-REPLAY"

DEBUG = False  # Safe fallback only; secure_server() callers pass debug= explicitly.


def resolve_existing_path(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[-1]
def _load_authorized_identity_public_key(encoded_public_key):
    if not isinstance(encoded_public_key, str):
        raise TypeError(
            "authorized station public key must be base64 text"
        )
    if not encoded_public_key:
        raise ValueError("authorized station public key must not be empty")
    try:
        encoded_ascii = encoded_public_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "authorized station public key must be ASCII base64"
        ) from exc
    try:
        public_key_bytes = base64.b64decode(
            encoded_ascii,
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "authorized station public key must be valid base64"
        ) from exc
    if base64.b64encode(public_key_bytes) != encoded_ascii:
        raise ValueError(
            "authorized station public key must use canonical base64"
        )
    if len(public_key_bytes) != 33:
        raise ValueError(
            "authorized station public key must be a 33-byte "
            "compressed P-256 point"
        )
    if public_key_bytes[0] not in (0x02, 0x03):
        raise ValueError(
            "authorized station public key must use compressed "
            "P-256 point encoding"
        )

    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(),
            public_key_bytes,
        )
    except ValueError:
        raise ValueError(
            "authorized station public key is not a valid P-256 point"
        ) from None
    canonical = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    if canonical != public_key_bytes:
        raise ValueError(
            "authorized station public key is not canonically encoded"
        )
    return public_key


base_dir = os.path.dirname(os.path.abspath(__file__))

auth_keys_path = resolve_existing_path(
    (
        "/etc/aismixer/authorized_keys.yaml",
        os.path.join(base_dir, "authorized_keys.yaml"),
    )
)

with open(auth_keys_path, 'r') as f:
    authorized_db = yaml.safe_load(f)

AUTHORIZED_KEYS = {
    entry["name"]: _load_authorized_identity_public_key(entry["pubkey"])
    for entry in authorized_db["authorized_clients"]
}

def _require_prepared_server_private_key(server_private_key):
    if server_private_key is None:
        raise RuntimeError(
            "UDPSEC server identity was not prepared before activation"
        )
    if not isinstance(server_private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("server identity private key must be an EC private key")
    if not isinstance(server_private_key.curve, ec.SECP256R1):
        raise ValueError("server identity private key must use P-256")
    return server_private_key


def _validate_positive_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _validate_positive_ttl(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be an integer or float")
    if not value > 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


@dataclass(frozen=True)
class _ExpiringRecord:
    key: object
    expires_at: float


@dataclass(frozen=True)
class _ExpiringSetAdmission:
    accepted: bool
    expired: int
    capacity_evicted: int


class _BoundedExpiringSet:
    def __init__(self, ttl, max_entries):
        self._ttl = ttl
        self._max_entries = max_entries
        self._live_by_key = {}
        self._expiry_order = deque()

    def __len__(self):
        return len(self._live_by_key)

    def _cleanup_expired(self, now):
        expired = 0
        while self._expiry_order:
            record = self._expiry_order[0]
            current = self._live_by_key.get(record.key)
            if current is not record:
                self._expiry_order.popleft()
                continue
            if record.expires_at > now:
                break
            self._expiry_order.popleft()
            del self._live_by_key[record.key]
            expired += 1
        return expired

    def _evict_oldest_live(self):
        while self._expiry_order:
            record = self._expiry_order.popleft()
            if self._live_by_key.get(record.key) is not record:
                continue
            del self._live_by_key[record.key]
            return 1
        raise RuntimeError("expiring-set ordering is inconsistent")

    def contains(self, key, now):
        expired = self._cleanup_expired(now)
        return key in self._live_by_key, expired

    def accept(self, key, now):
        expired = self._cleanup_expired(now)
        if key in self._live_by_key:
            return _ExpiringSetAdmission(False, expired, 0)

        capacity_evicted = 0
        if len(self._live_by_key) >= self._max_entries:
            capacity_evicted = self._evict_oldest_live()

        record = _ExpiringRecord(key=key, expires_at=now + self._ttl)
        self._live_by_key[key] = record
        self._expiry_order.append(record)
        return _ExpiringSetAdmission(True, expired, capacity_evicted)

    def discard_all(self):
        discarded = len(self._live_by_key)
        self._live_by_key.clear()
        self._expiry_order.clear()
        return discarded


class _DataNonceAdmission(Enum):
    ACCEPTED = "accepted"
    REPLAY = "replay"
    EXHAUSTED = "exhausted"
    STALE = "stale"


class _BoundedNonceSet:
    """Retain DATA nonces until their owning traffic-key epoch ends."""

    def __init__(self, max_entries):
        self._max_entries = max_entries
        self._live_by_key = set()

    def __len__(self):
        return len(self._live_by_key)

    def contains(self, key):
        return key in self._live_by_key

    def admit(self, key):
        if key in self._live_by_key:
            return _DataNonceAdmission.REPLAY
        if len(self._live_by_key) >= self._max_entries:
            return _DataNonceAdmission.EXHAUSTED
        self._live_by_key.add(key)
        return _DataNonceAdmission.ACCEPTED

    def discard_all(self):
        discarded = len(self._live_by_key)
        self._live_by_key.clear()
        return discarded


class _EndpointToken:
    """Opaque identity token for one physical listener incarnation."""

    __slots__ = ()


def _new_endpoint_token():
    return _EndpointToken()


# The one process-wide registry every SecureState owner shares for
# session_handle/assembly_namespace reservation. See
# core/session_identity_registry.py for the full design rationale: unlike
# an unchecked os.urandom() draw, this checks-and-reserves atomically under
# its own lock, so two independent owners -- including two genuinely
# concurrent real OS threads, which this project's own test suite exercises
# for multi-listener isolation -- can never both believe they hold the same
# value. Every SecureState instance constructed through the normal
# `import aismixer_secure` path shares this exact object (Python's import
# system caches by module name); a test harness that deliberately loads
# this module a second time under a synthetic name gets its own
# independent registry, exactly like it gets its own independent
# `AUTHORIZED_KEYS` and `secure_state` -- a fully separate simulated
# process for everything, not a partial gap in identifier sharing alone.
_SESSION_IDENTITY_REGISTRY = SessionIdentityRegistry()


@dataclass(frozen=True)
class _EndpointPeerKey:
    """Process-local identity for one listener/remote-peer relation."""

    endpoint_token: _EndpointToken
    peer_address: object

    def __post_init__(self):
        if not isinstance(self.endpoint_token, _EndpointToken):
            raise TypeError("endpoint_token must be an _EndpointToken")


def _require_endpoint_peer_key(relation_key):
    if not isinstance(relation_key, _EndpointPeerKey):
        raise TypeError("relation_key must be an _EndpointPeerKey")
    return relation_key


@dataclass(frozen=True)
class _EndpointSessionKey:
    """Process-local identity for one listener/session-locator relation.

    This -- not the remote peer tuple -- is the authoritative key for an
    established `LogicalSession`. `endpoint_token` scopes identity to one
    physical listener incarnation exactly as `_EndpointPeerKey` already does
    for pending relations; the same raw locator bytes on a different
    `endpoint_token` is an independent identity.
    """

    endpoint_token: _EndpointToken
    session_locator: bytes

    def __post_init__(self):
        if not isinstance(self.endpoint_token, _EndpointToken):
            raise TypeError("endpoint_token must be an _EndpointToken")
        if not isinstance(self.session_locator, bytes):
            raise TypeError("session_locator must be bytes")
        if len(self.session_locator) != SESSION_LOCATOR_BYTES:
            raise ValueError(
                "session_locator must be exactly "
                f"{SESSION_LOCATOR_BYTES} bytes"
            )


def _require_endpoint_session_key(session_key):
    if not isinstance(session_key, _EndpointSessionKey):
        raise TypeError("session_key must be an _EndpointSessionKey")
    return session_key


def _require_session_locator(value):
    if not isinstance(value, bytes):
        raise TypeError("session_locator must be bytes")
    if len(value) != SESSION_LOCATOR_BYTES:
        raise ValueError(
            f"session_locator must be exactly {SESSION_LOCATOR_BYTES} bytes"
        )
    return value


class SessionLocatorExhaustedError(RuntimeError):
    """Raised when secure-random session-locator generation cannot find a
    free value within the bounded attempt limit.

    This is a fail-closed condition: callers must not retry unboundedly or
    fall back to a weaker/derived locator.
    """


class SessionLocatorCollisionError(RuntimeError):
    """Raised when `install_session`/`install_pending_session` is given a
    `session_locator` that already identifies a *different* live pending
    or active session on the same `endpoint_token`.

    Locator uniqueness is a `SecureState` invariant enforced at the point
    of installation, not merely a convention observed by
    `generate_session_locator()`: a direct/internal caller that supplies
    an already-claimed locator must fail closed here rather than silently
    overwriting the existing session's `_sessions`/`_pending_locator_owners`
    entry (which would orphan the original session while leaving stale
    bookkeeping, or leave two live pending objects sharing one ownership
    record). Callers should mint locators via `generate_session_locator()`,
    which already avoids this by construction.
    """


class SecureStateClosedError(RuntimeError):
    """R5/F5: raised by `install_session`/`install_pending_session` once
    this owner's `close()` has run.

    A closed owner must not silently accept new sessions or recreate
    state -- there is no reopen contract; construct a fresh
    `SecureState()` for a new owner.
    """


@dataclass
class CryptoEpoch:
    """One authenticated ECDHE handshake's key material and replay ledger.

    A crypto epoch is replaceable as a unit: a successful authenticated
    re-handshake (rekey) installs a fresh ``CryptoEpoch``. In this stage that
    replacement still happens by replacing the whole owning `LogicalSession`
    (see `promote_pending_session`), but the crypto state is now represented
    as an explicit, independently referenceable object so a later stage can
    swap only the epoch on an unchanged `LogicalSession` instead.
    """

    client_to_server_aesgcm: AESGCM
    server_to_client_aesgcm: AESGCM
    seen_data_nonces: _BoundedNonceSet
    created_at: float


def _structured_paths_match(a, b):
    """Compare two raw socket address tuples by structured identity, not
    by display formatting.

    Delegates to the shared `core.sockaddr_identity` model (also used by
    the client's `remote_addresses_match`) so both sides of the protocol
    apply identical, strict semantics: IPv4 is ``(family, ip, port)``;
    IPv6 is ``(family, ip, port, scope_id)`` with ``flowinfo`` excluded
    and a missing scope defaulting to 0 (never a wildcard). A malformed or
    ambiguous sockaddr raises `MalformedSockaddrError` rather than being
    treated as equivalent to a valid address.
    """

    return sockaddrs_match(a, b)


def _canonical_active_relation_key(relation_key):
    """Canonicalize one `_EndpointPeerKey` for `_relation_index` lookups
    so that active-relation identity agrees with `_structured_paths_match`
    exactly -- built from the same `normalize_sockaddr` both use, so a
    2-tuple and a native 4-tuple IPv6 address that identify the same path
    (per `_structured_paths_match`) always hash to the same relation-index
    entry, and flowinfo never distinguishes an otherwise-identical
    relation.

    `_relation_index` is a plain dict keyed by `_EndpointPeerKey`, whose
    equality is raw tuple equality over the *entire* Python socket address
    -- including flowinfo, and treating a bare 2-tuple as distinct from an
    equivalent 4-tuple. Without this normalization, two packets from the
    same IPv6 relation that merely carry different flowinfo values (or a
    scope-less vs. scope-0 shape) would hash to different relation-index
    entries even though the wrong-path check (`_structured_paths_match`)
    treats them as the same path, breaking same-relation replacement
    detection and assembly-namespace carry-forward for exactly the
    link-local/flow-labeled traffic this index exists to serve.

    Pending handshake lookup (`SecureState._pending_sessions`,
    `_pending_locator_owners`) is intentionally exact/tuple-bound per the
    UDPSEC V2 spec and must never be passed through this helper.
    """

    return _EndpointPeerKey(
        relation_key.endpoint_token,
        normalize_sockaddr(relation_key.peer_address),
    )


@dataclass
class PathState:
    """The established session's currently authoritative outbound path.

    Only `active_path` exists in this stage. Candidate/retired path tracking
    and any path-validation/migration behavior are intentionally NOT
    implemented yet; this type exists so a later stage can add them without
    another ownership refactor.
    """

    active_path: object


@dataclass
class LogicalSession:
    """One authenticated, established UDPSEC relation.

    The wire/session lookup is now tuple-independent: `_session_key` (an
    `_EndpointSessionKey` of endpoint token + server-minted session locator)
    is the authoritative identity for `SecureState._sessions`, replacing the
    endpoint-token/peer-address relation used before UDPSEC protocol
    revision 2. The remote peer tuple survives only as transport/path state
    in `path_state.active_path`; it is no longer part of session identity.

    The session's cryptographic state and its outbound path are owned by
    distinct `current_epoch`/`path_state` objects instead of being flat
    fields, and the session carries two separate process-local identifiers
    that must never be conflated:

    - `session_handle` identifies this exact `LogicalSession` *object*
      incarnation. It is fresh on every construction, including a same-
      relation authenticated replacement (today's rekey still replaces the
      whole object -- and therefore also always mints a fresh
      `session_locator`), and is never reused merely because the relation
      matches.
    - `assembly_namespace` identifies the secure multipart continuity
      lineage. It is normally also fresh, but a same-relation authenticated
      replacement of the *same authenticated station* carries it forward
      from the session it replaces, so an in-flight multipart AIS message
      is not split by a routine rekey. It never survives a genuine session
      death (expiry, close, capacity eviction, nonce exhaustion, restart) or
      a replacement whose authenticated station differs. This is a
      transitional mechanism for the current stage, where rekey still
      replaces the whole `LogicalSession`; once a later stage lets rekey
      replace only `current_epoch` on an unchanged `LogicalSession`
      (keeping the same `session_locator`), the assembly namespace will
      remain stable simply because the session object itself persists, and
      this field's reuse logic will no longer be needed.

    Both fields are opaque `bytes` values reserved through the shared
    `core.session_identity_registry.SessionIdentityRegistry` singleton
    (`_SESSION_IDENTITY_REGISTRY` below; see that module's docstring for
    why a checked random reservation, not a shared counter, is used):
    neither is stringified except `assembly_namespace` into the
    `udpsec-assembly:` assembler key, and neither is ever transmitted.
    """

    _session_key: _EndpointSessionKey
    station_id: str
    created_at: float
    last_seen: float
    session_handle: bytes
    assembly_namespace: bytes
    current_epoch: CryptoEpoch
    path_state: PathState


@dataclass
class _PendingSecureSession:
    _relation_key: _EndpointPeerKey
    _address: object
    station_id: str
    session_locator: bytes
    created_at: float
    current_epoch: CryptoEpoch


@dataclass(frozen=True)
class SecureStateStats:
    handshake_replay_accepted: int
    handshake_replay_rejected: int
    handshake_replay_expired: int
    handshake_replay_capacity_evicted: int

    sessions_created: int
    sessions_replaced: int
    sessions_touched: int
    sessions_expired: int
    sessions_closed: int
    sessions_capacity_evicted: int

    pending_sessions_created: int
    pending_sessions_replaced: int
    pending_sessions_promoted: int
    pending_sessions_expired: int
    pending_sessions_capacity_evicted: int
    pending_sessions_closed: int

    data_nonces_accepted: int
    data_nonce_replays: int
    data_nonces_expired: int
    data_nonces_capacity_evicted: int
    data_nonce_exhaustions: int
    data_nonces_session_discarded: int

    current_handshake_replays: int
    peak_handshake_replays: int
    current_sessions: int
    peak_sessions: int
    current_pending_sessions: int
    peak_pending_sessions: int
    current_data_nonces: int
    peak_data_nonces: int


class SecureState:
    def __init__(
        self,
        session_ttl=SESSION_TTL_SECONDS,
        max_sessions=SESSION_MAX,
        handshake_replay_ttl=HANDSHAKE_REPLAY_TTL_SECONDS,
        handshake_replay_max=HANDSHAKE_REPLAY_MAX,
        data_nonce_max_per_session=DATA_NONCE_MAX_PER_SESSION,
        pending_session_ttl=PENDING_SESSION_TTL_SECONDS,
        max_pending_sessions=PENDING_SESSION_MAX,
        clock=None,
    ):
        # R5/F2: an OPTIONAL authoritative monotonic clock this owner
        # trusts as a floor under every caller-supplied `now`. `None`
        # (the default) preserves the exact prior behavior -- every
        # existing caller and test that never configures this continues
        # to have its own supplied `now` taken as-is, with zero behavior
        # change. When configured (production's module-level
        # `secure_state` below passes `time.monotonic`), a stale `now`
        # sampled before a delay (a different thread, a slow prior
        # crypto/parsing step, or simply an old async task holding a
        # stale session reference) can no longer make an already-expired
        # owner appear live: every method that takes `now` runs it
        # through `_authoritative_now()` first, which floors it at this
        # clock's current reading. `now` itself is never discarded --
        # a caller supplying a value AHEAD of this clock (routine
        # deterministic testing, or a legitimately fresher observation)
        # is respected unchanged; only a value BEHIND it is corrected.
        # A `SecureState` sharing `_SESSION_IDENTITY_REGISTRY` with other
        # owners must use a clock domain compatible with theirs -- the
        # registry's own retirement bookkeeping is keyed by whatever
        # `now` each owner's removals pass it, so two owners configured
        # with incompatible clocks (e.g. one real, one a fake test clock
        # far in the past or future) would corrupt each other's
        # retirement windows. Every owner sharing the process-wide
        # default registry in production shares the same real
        # `time.monotonic`; a test constructing an isolated owner with a
        # fake clock must not also feed values into the shared registry
        # through a DIFFERENT owner using a real or differently-scaled
        # clock.
        self._clock = clock
        # R5/F5: set exactly once, by `close()`, this owner's own explicit
        # end-of-life marker -- see that method.
        self._closed = False
        self._session_ttl = _validate_positive_ttl(
            "session_ttl", session_ttl)
        self._max_sessions = _validate_positive_int(
            "max_sessions", max_sessions)
        self._pending_session_ttl = _validate_positive_ttl(
            "pending_session_ttl", pending_session_ttl)
        self._max_pending_sessions = _validate_positive_int(
            "max_pending_sessions", max_pending_sessions)
        self._handshake_replay_ttl = _validate_positive_ttl(
            "handshake_replay_ttl", handshake_replay_ttl)
        self._handshake_replay_max = _validate_positive_int(
            "handshake_replay_max", handshake_replay_max)
        self._data_nonce_max_per_session = _validate_positive_int(
            "data_nonce_max_per_session", data_nonce_max_per_session)

        # One reentrant lock protects every composite state transaction
        # below (replay admission, pending/active install/replace/remove,
        # locator reservation, promotion, nonce admission, capacity
        # eviction, and statistics snapshots). It is reentrant because
        # several public entry points call other public entry points as
        # an internal implementation detail (e.g. `install_session` and
        # `promote_pending_session` both call `cleanup_expired_sessions`;
        # `accept_data_nonce` calls `admit_data_nonce`) -- a plain Lock
        # would deadlock a thread against itself on those paths. Every
        # `_`-prefixed helper below relies on its caller already holding
        # this lock rather than acquiring its own: it must never be called
        # directly from outside a public method's `with self._lock:`
        # block. The lock is held only for in-memory dict/set/counter
        # work -- never across `_SESSION_IDENTITY_REGISTRY` calls that
        # would need to re-enter it, never across ECDHE/AEAD/JSON/network
        # I/O, and never across an `await` -- so lock order is always
        # this lock first, the identity registry's own internal lock (if
        # any) second, never the reverse.
        self._lock = threading.RLock()
        self._handshake_replays = _BoundedExpiringSet(
            self._handshake_replay_ttl,
            self._handshake_replay_max,
        )
        # Authoritative established-session store: keyed by
        # `_EndpointSessionKey` (endpoint token + session locator), NOT by
        # remote peer tuple.
        self._sessions = OrderedDict()
        # Pending (unconfirmed handshake) store: still keyed by the exact
        # endpoint-token/peer-address relation -- migration of an
        # unconfirmed handshake is out of scope; a NAT/path change during
        # the pending handshake causes ordinary handshake failure/retry.
        self._pending_sessions = OrderedDict()
        # Bounded secondary index: current relation -> the active session
        # key currently occupying it. NOT authentication/lookup authority
        # for DATA traffic -- used only for same-relation replacement
        # detection, assembly-namespace carry-forward, and diagnostics. Kept
        # exactly one-to-one with live active sessions by every removal path.
        self._relation_index = {}
        # Bounded collision-prevention index for pending locators: maps
        # (endpoint_token, session_locator) -> the exact pending relation
        # that reserved it. Active-session locator collision is checked
        # directly against `self._sessions` (see `_locator_is_free`), since
        # that store is already locator-keyed.
        self._pending_locator_owners = {}
        # Expiry authority is deliberately SEPARATE from `_sessions`'/
        # `_pending_sessions`' OrderedDict position, which exists only for
        # LRU/capacity-eviction ordering. `_sessions`/`_pending_sessions`
        # are reordered (via `move_to_end` on touch, or plain insertion) at
        # the moment each state transaction actually COMMITS under this
        # lock -- but two transactions racing to commit do not necessarily
        # commit in the same order as the `now` values they were called
        # with (a thread can sample a LATER `now` yet still acquire this
        # lock and commit BEFORE a thread that sampled an EARLIER `now`).
        # An OrderedDict-front-prefix expiry scan silently assumes commit
        # order tracks timestamp order; when it does not, a genuinely
        # expired entry can sit behind a newer one at the front and never
        # be found by that scan, letting expired active/pending state be
        # treated as live. These two min-heaps are ordered strictly by
        # deadline VALUE, not by insertion/commit order, so
        # `cleanup_expired_sessions`/`cleanup_expired_pending_sessions`
        # always find every genuinely due entry regardless of commit
        # order. Entries are lazily validated against the live store when
        # popped (see those methods) rather than proactively invalidated
        # on every touch, which keeps each heap's size bounded by roughly
        # the number of live entries rather than by total touch volume.
        self._session_expiry_heap = []
        self._pending_expiry_heap = []
        # Heap entries are `(deadline, sequence, key)`. `_EndpointSessionKey`
        # and `_EndpointPeerKey` support equality but not ordering, so two
        # entries with an equal `deadline` (routine -- many sessions can
        # share one `now` and `session_ttl`) would otherwise make `heapq`
        # fall through to comparing the keys and raise `TypeError`. The
        # monotonically increasing `sequence` is unique across every push
        # to either heap, so it alone breaks every tie and the key is
        # never actually compared.
        self._expiry_heap_sequence = itertools.count()
        # `session_handle` identifies one LogicalSession object incarnation
        # and is always fresh. `assembly_namespace` identifies secure
        # multipart continuity lineage and may be carried forward across a
        # same-relation, same-station authenticated replacement. Both are
        # process-local values, never transmitted, reserved through the
        # shared `_SESSION_IDENTITY_REGISTRY` (see
        # `core.session_identity_registry` for why this is a checked random
        # reservation rather than a shared counter, and how its own short
        # internal lock is what keeps two independent SecureState owners --
        # including different OS threads -- from ever holding the same
        # value at once).

        self._handshake_replay_accepted = 0
        self._handshake_replay_rejected = 0
        self._handshake_replay_expired = 0
        self._handshake_replay_capacity_evicted = 0

        self._sessions_created = 0
        self._sessions_replaced = 0
        self._sessions_touched = 0
        self._sessions_expired = 0
        self._sessions_closed = 0
        self._sessions_capacity_evicted = 0

        self._pending_sessions_created = 0
        self._pending_sessions_replaced = 0
        self._pending_sessions_promoted = 0
        self._pending_sessions_expired = 0
        self._pending_sessions_capacity_evicted = 0
        self._pending_sessions_closed = 0

        self._data_nonces_accepted = 0
        self._data_nonce_replays = 0
        # Retained for compatibility; DATA nonce records no longer expire or
        # undergo live-record capacity eviction within a traffic-key epoch.
        self._data_nonces_expired = 0
        self._data_nonces_capacity_evicted = 0
        self._data_nonce_exhaustions = 0
        self._data_nonces_session_discarded = 0

        self._current_data_nonces = 0
        self._peak_handshake_replays = 0
        self._peak_sessions = 0
        self._peak_pending_sessions = 0
        self._peak_data_nonces = 0

    def _authoritative_now(self, now):
        """Floor a caller-supplied `now` at this owner's configured
        authoritative clock, if any (see `__init__`). Called once, at the
        top of every public method that takes `now`, so every downstream
        use within that call -- expiry comparison, `last_seen`/
        `created_at` recording, heap deadline computation, registry
        release timestamps -- is consistent within that one transaction.
        Returns `now` unchanged when no clock is configured (the default),
        or when `now` is already at or ahead of the clock's own reading.
        """
        if self._clock is None:
            return now
        return max(now, self._clock())

    def stats(self) -> SecureStateStats:
        with self._lock:
            return SecureStateStats(
                handshake_replay_accepted=self._handshake_replay_accepted,
                handshake_replay_rejected=self._handshake_replay_rejected,
                handshake_replay_expired=self._handshake_replay_expired,
                handshake_replay_capacity_evicted=(
                    self._handshake_replay_capacity_evicted
                ),
                sessions_created=self._sessions_created,
                sessions_replaced=self._sessions_replaced,
                sessions_touched=self._sessions_touched,
                sessions_expired=self._sessions_expired,
                sessions_closed=self._sessions_closed,
                sessions_capacity_evicted=self._sessions_capacity_evicted,
                pending_sessions_created=self._pending_sessions_created,
                pending_sessions_replaced=self._pending_sessions_replaced,
                pending_sessions_promoted=self._pending_sessions_promoted,
                pending_sessions_expired=self._pending_sessions_expired,
                pending_sessions_capacity_evicted=(
                    self._pending_sessions_capacity_evicted
                ),
                pending_sessions_closed=self._pending_sessions_closed,
                data_nonces_accepted=self._data_nonces_accepted,
                data_nonce_replays=self._data_nonce_replays,
                data_nonces_expired=self._data_nonces_expired,
                data_nonces_capacity_evicted=(
                    self._data_nonces_capacity_evicted
                ),
                data_nonce_exhaustions=self._data_nonce_exhaustions,
                data_nonces_session_discarded=(
                    self._data_nonces_session_discarded
                ),
                current_handshake_replays=len(self._handshake_replays),
                peak_handshake_replays=self._peak_handshake_replays,
                current_sessions=len(self._sessions),
                peak_sessions=self._peak_sessions,
                current_pending_sessions=len(self._pending_sessions),
                peak_pending_sessions=self._peak_pending_sessions,
                current_data_nonces=self._current_data_nonces,
                peak_data_nonces=self._peak_data_nonces,
            )

    def accept_handshake_replay(self, key, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._closed:
                # R6/F5: a terminal owner must not accept new replay
                # records either -- exactly like `install_session`/
                # `install_pending_session`, there is no reopen contract.
                # `close()` has already discarded every existing replay
                # record; treating a post-close ClientHello as an ordinary
                # rejection (not raising) matches every caller's existing
                # "print and continue" handling of a rejected handshake.
                self._handshake_replay_rejected += 1
                return False
            admission = self._handshake_replays.accept(key, now)
            self._handshake_replay_expired += admission.expired
            self._handshake_replay_capacity_evicted += (
                admission.capacity_evicted
            )
            if not admission.accepted:
                self._handshake_replay_rejected += 1
                return False

            self._handshake_replay_accepted += 1
            self._peak_handshake_replays = max(
                self._peak_handshake_replays,
                len(self._handshake_replays),
            )
            return True

    def _discard_session_nonces(self, session):
        discarded_nonces = session.current_epoch.seen_data_nonces.discard_all()
        self._current_data_nonces -= discarded_nonces
        self._data_nonces_session_discarded += discarded_nonces

    def _remove_session(self, session_key, reason, now):
        # Canonicalize before popping: `_canonical_active_relation_key` can
        # raise `MalformedSockaddrError` for a genuinely malformed
        # `active_path` (unreachable for a session whose path was already
        # validated at install/promotion time, since that value is never
        # mutated afterward -- but this ordering makes the invariant hold
        # by construction rather than by relying on that argument alone).
        # A raise here must leave `_sessions` and every other store
        # untouched, not a session already popped with nothing else done.
        session = self._sessions[session_key]
        relation_key = _canonical_active_relation_key(
            _EndpointPeerKey(
                session_key.endpoint_token, session.path_state.active_path
            )
        )
        del self._sessions[session_key]
        if self._relation_index.get(relation_key) == session_key:
            del self._relation_index[relation_key]
        self._discard_session_nonces(session)
        # session_handle has exactly one owner for its whole life and is
        # released immediately. assembly_namespace may still be claimed by
        # a replacement installed later in this same transaction (see
        # `_reused_or_fresh_assembly_namespace`, always called before this
        # removal for a "replaced" reason) or by a downstream assembler
        # group that outlives this exact session -- release() accounts for
        # both by reference count and retirement window rather than
        # deleting the reservation outright. See
        # core/session_identity_registry.py.
        _SESSION_IDENTITY_REGISTRY.release(session.session_handle, now)
        _SESSION_IDENTITY_REGISTRY.release(session.assembly_namespace, now)

        if reason == "expired":
            self._sessions_expired += 1
        elif reason == "closed":
            self._sessions_closed += 1
        elif reason == "capacity":
            self._sessions_capacity_evicted += 1
        elif reason == "replaced":
            self._sessions_replaced += 1
        elif reason == "nonce_exhausted":
            self._data_nonce_exhaustions += 1
        else:
            raise ValueError(f"Unknown session removal reason: {reason}")
        return session

    def _remove_pending_session(self, relation_key, reason):
        pending = self._pending_sessions.pop(relation_key)
        locator_key = (relation_key.endpoint_token, pending.session_locator)
        if self._pending_locator_owners.get(locator_key) == relation_key:
            del self._pending_locator_owners[locator_key]
        self._discard_session_nonces(pending)

        if reason == "expired":
            self._pending_sessions_expired += 1
        elif reason == "capacity":
            self._pending_sessions_capacity_evicted += 1
        elif reason == "replaced":
            self._pending_sessions_replaced += 1
        elif reason == "nonce_exhausted":
            self._data_nonce_exhaustions += 1
        elif reason == "closed":
            self._pending_sessions_closed += 1
        else:
            raise ValueError(
                f"Unknown pending-session removal reason: {reason}"
            )
        return pending

    def _push_session_expiry(self, session):
        # R6: the pushed tuple carries the exact `session` OBJECT, not
        # just its key -- see `cleanup_expired_sessions` for why identity,
        # not mere key presence, is what a popped entry must be validated
        # against.
        heapq.heappush(
            self._session_expiry_heap,
            (
                session.last_seen + self._session_ttl,
                next(self._expiry_heap_sequence),
                session._session_key,
                session,
            ),
        )

    def _push_pending_expiry(self, pending):
        # R6: same identity-carrying shape as `_push_session_expiry`, and
        # for a stronger reason here: `_relation_key` is NOT fresh across
        # a same-relation pending replacement (unlike an active session's
        # locator-derived key), so without the object itself a popped
        # entry could not tell "this candidate was replaced" from "this
        # candidate is still exactly the one that occupies this relation".
        heapq.heappush(
            self._pending_expiry_heap,
            (
                pending.created_at + self._pending_session_ttl,
                next(self._expiry_heap_sequence),
                pending._relation_key,
                pending,
            ),
        )

    def cleanup_expired_sessions(self, now):
        with self._lock:
            now = self._authoritative_now(now)
            expired = []
            while self._session_expiry_heap:
                deadline, _sequence, session_key, pushed_session = (
                    self._session_expiry_heap[0]
                )
                if deadline > now:
                    break
                heapq.heappop(self._session_expiry_heap)
                session = self._sessions.get(session_key)
                # R6: identity, not mere key presence, is authoritative.
                # `session_key` is a fresh locator on every replacement, so
                # an orphaned entry usually already fails a plain
                # `is None` check -- but a session_key value CAN
                # legitimately be reused after a long enough retirement
                # (a fresh random locator draw colliding with a long-gone
                # one), and at that point a stale entry's `session_key`
                # would resolve to a completely unrelated, newer
                # incarnation. Comparing the exact object this entry was
                # pushed for -- not just what currently occupies its key
                # -- means a superseded/replaced/reused-key entry is
                # always discarded outright here, never mistaken for
                # authority to inspect or reschedule whatever object
                # actually occupies that key now (this is also what keeps
                # heap size bounded under sustained same-relation
                # replacement churn: a discarded entry is never re-pushed
                # on behalf of an object it was never pushed for).
                if session is not pushed_session:
                    continue  # superseded, reused key, or removed outright
                current_deadline = session.last_seen + self._session_ttl
                if current_deadline > now:
                    # This entry predates a later touch: the session's
                    # real current deadline is not actually due yet. Push
                    # one fresh entry reflecting that real deadline and
                    # keep scanning -- another entry genuinely due now may
                    # still be sitting behind this one in heap order.
                    heapq.heappush(
                        self._session_expiry_heap,
                        (
                            current_deadline,
                            next(self._expiry_heap_sequence),
                            session_key,
                            session,
                        ),
                    )
                    continue
                self._remove_session(session_key, "expired", now)
                expired.append(session_key)
            # Bounded registry housekeeping piggybacked on this already-
            # existing monotonic cleanup pass, not a dedicated thread or a
            # per-packet full-registry scan: purge only retired-and-elapsed
            # identifier reservations (see
            # core.session_identity_registry.SessionIdentityRegistry).
            _SESSION_IDENTITY_REGISTRY.purge_expired(now)
            return expired

    def cleanup_expired_pending_sessions(self, now):
        with self._lock:
            now = self._authoritative_now(now)
            expired = []
            while self._pending_expiry_heap:
                deadline, _sequence, relation_key, pushed_pending = (
                    self._pending_expiry_heap[0]
                )
                if deadline > now:
                    break
                heapq.heappop(self._pending_expiry_heap)
                pending = self._pending_sessions.get(relation_key)
                # R6: unlike an active session's locator-derived key, a
                # pending relation_key is NOT fresh across a same-relation
                # replacement -- a new ServerHello at the same relation
                # keeps the exact same `_EndpointPeerKey`. Without this
                # identity check, a stale entry pushed for an earlier,
                # already-replaced candidate would still resolve
                # `relation_key` to whatever candidate currently occupies
                # it and could recompute/reschedule THAT candidate's
                # deadline on the strength of an entry that was never
                # pushed for it -- exactly the previously-reported defect,
                # where continuous same-relation replacement grew this
                # heap without bound even though only one pending
                # candidate was ever live at a time. `pending.created_at`
                # never changes after installation (pending candidates
                # are never touched/extended the way active sessions are),
                # so an identity match here always means this entry's
                # deadline is still exactly correct -- no recompute is
                # ever needed, unlike the active-session heap above.
                if pending is not pushed_pending:
                    continue  # superseded, reused key, or removed outright
                self._remove_pending_session(relation_key, "expired")
                expired.append(relation_key)
            return expired

    async def run_periodic_maintenance(
        self,
        interval=IDLE_MAINTENANCE_INTERVAL_SECONDS,
    ):
        """F3: run this owner's expiry and retirement-purge housekeeping
        on a timer, independent of any packet arriving to trigger it as a
        side effect.

        This is NOT security-relevant on its own: `_secure_server_loop`
        and every other public entry point already call
        `cleanup_expired_sessions`/`cleanup_expired_pending_sessions`
        before treating any active/pending state as live, so an expired
        owner is correctly rejected regardless of when this last ran. Its
        only purpose is bounding how long already-expired state and
        retired identifier reservations can sit un-reclaimed on a fully
        idle owner (no packets on any listener sharing it) -- otherwise
        that housekeeping never runs at all until the next packet.

        R5/F2: samples THIS owner's own configured authoritative clock
        (falling back to real `time.monotonic` if none is configured) --
        deliberately not an independently injectable parameter, so
        maintenance can never observe a different, incoherent clock
        domain than every other decision this owner makes via
        `_authoritative_now()`. A test wanting deterministic maintenance
        behavior configures `SecureState(clock=...)` at construction, the
        same way it would to test any other authoritative-clock behavior
        on this owner.

        Intended to be spawned as ONE independent supervised task per
        `SecureState` owner (not per physical listener -- every listener
        sharing one owner already benefits from a single task cleaning up
        that shared owner's state), with a lifetime managed by the
        caller exactly like any other supervised background task.
        Cancelling it (or any one listener's own task) does not affect
        any other owner's maintenance task, and this method does not
        start, own, or need to know about any listener socket. Runs
        until cancelled.
        """
        monotonic_now = time.monotonic if self._clock is None else self._clock
        while True:
            await asyncio.sleep(interval)
            now = monotonic_now()
            self.cleanup_expired_sessions(now)
            self.cleanup_expired_pending_sessions(now)

    def _fresh_session_handle(self):
        """Always mint a new, process-wide-unique LogicalSession-incarnation
        identity, atomically reserved in `_SESSION_IDENTITY_REGISTRY`.
        Never reused, including across a same-relation authenticated
        replacement, and never repeated by a different SecureState owner
        in this process. Released exactly once, in `_remove_session`, when
        this exact LogicalSession incarnation is removed for any reason."""
        return _SESSION_IDENTITY_REGISTRY.reserve()

    def _active_session_at_canonical_relation(self, canonical_relation_key):
        """Same as `_active_session_at_relation`, but takes an already-
        canonical key: callers performing more than one relation-indexed
        lookup or mutation within a single state transaction
        (`install_session`, `promote_pending_session`) canonicalize the raw
        `relation_key` exactly once and pass the result through, rather
        than each step independently (and, for a malformed peer address,
        redundantly-fallibly) re-canonicalizing the same input."""
        session_key = self._relation_index.get(canonical_relation_key)
        if session_key is None:
            return None
        return self._sessions.get(session_key)

    def _active_session_at_relation(self, relation_key):
        """Return the LogicalSession currently occupying this exact
        relation via the bounded secondary relation index, or None.

        This index is NOT authentication or lookup authority for DATA
        traffic -- it exists only for same-relation replacement detection,
        assembly-namespace carry-forward, and diagnostics. The lookup key
        is canonicalized so IPv6 flowinfo never distinguishes an
        otherwise-identical relation, matching `_structured_paths_match`.
        """
        return self._active_session_at_canonical_relation(
            _canonical_active_relation_key(relation_key)
        )

    def _remove_active_at_canonical_relation_if_present(
        self, canonical_relation_key, now
    ):
        session_key = self._relation_index.get(canonical_relation_key)
        if session_key is None:
            return False
        self._remove_session(session_key, "replaced", now)
        return True

    def _remove_active_at_relation_if_present(self, relation_key, now):
        return self._remove_active_at_canonical_relation_if_present(
            _canonical_active_relation_key(relation_key), now
        )

    def _reused_or_fresh_assembly_namespace(
        self, canonical_relation_key, station_id
    ):
        """Preserve secure multipart continuity across a same-relation
        authenticated replacement of the SAME authenticated station only.

        A currently live session at `canonical_relation_key` (via the
        relation index) whose authenticated `station_id` matches
        contributes its `assembly_namespace` forward -- `claim()`ing an
        additional live reference to the SAME reservation (not drawing a
        new one) in `_SESSION_IDENTITY_REGISTRY`, so an in-flight multipart
        AIS message is not split by a routine rekey. Every other case -- a
        different authenticated station at the same relation, a genuinely
        different/new relation, or no live session at all (already-expired,
        closed, capacity-evicted, or nonce-exhausted state) -- reserves a
        fresh, process-wide-unique namespace that no other SecureState
        owner in this process can ever also mint. There is no historical
        cache of removed relations/stations/namespaces: a retired
        namespace's reservation is purged after its retirement window (see
        `core.session_identity_registry`), not kept forever.

        Takes an already-canonical relation key, not a raw `relation_key`:
        callers (`install_session`, `promote_pending_session`) canonicalize
        the raw key exactly once per transaction and pass the result to
        every relation-indexed step, rather than each step independently
        re-canonicalizing (and re-risking `MalformedSockaddrError` on) the
        same input.

        Must be called before the old session (if any) is removed: the
        claim/reserve here and that removal's own release in
        `_remove_session` both touch the same registry entry, and this
        call reading `_active_session_at_canonical_relation` needs the old
        session to still be live to decide reuse-vs-fresh correctly.
        """
        replaced = self._active_session_at_canonical_relation(
            canonical_relation_key
        )
        if replaced is not None and replaced.station_id == station_id:
            _SESSION_IDENTITY_REGISTRY.claim(replaced.assembly_namespace)
            return replaced.assembly_namespace
        return _SESSION_IDENTITY_REGISTRY.reserve()

    def _locator_is_free(self, endpoint_token, candidate):
        if (endpoint_token, candidate) in self._pending_locator_owners:
            return False
        if _EndpointSessionKey(endpoint_token, candidate) in self._sessions:
            return False
        return True

    def _pending_exclusively_owns_locator(self, relation_key, candidate):
        """Pure query, never a mutation: True only when `candidate` is
        both free of any live active session AND currently reserved by
        this exact pending relation -- not by another pending candidate,
        and not missing or pointing elsewhere.

        Unlike `_locator_is_free()`, this deliberately treats the
        pending's OWN reservation as expected rather than as a
        collision, so promotion can validate self-ownership before
        performing any destructive state transition instead of having to
        temporarily release the reservation just to make
        `_locator_is_free()` return True.
        """
        if _EndpointSessionKey(
            relation_key.endpoint_token, candidate
        ) in self._sessions:
            return False
        owner = self._pending_locator_owners.get(
            (relation_key.endpoint_token, candidate)
        )
        return owner == relation_key

    def generate_session_locator(self, endpoint_token):
        """Generate a fresh, unique 16-byte session locator for one
        physical listener incarnation.

        Uniqueness is checked against every currently live pending AND
        active session on this exact `endpoint_token` -- the same raw
        locator bytes on a different endpoint token is an independent
        identity and is not considered a collision. Bounded retry; fails
        closed (`SessionLocatorExhaustedError`) rather than spinning
        forever or falling back to a weaker value. This method does not
        mutate state: reservation happens when the caller actually installs
        the pending/active session with the returned locator.
        """
        if not isinstance(endpoint_token, _EndpointToken):
            raise TypeError("endpoint_token must be an _EndpointToken")
        with self._lock:
            for _ in range(SESSION_LOCATOR_GENERATION_ATTEMPTS):
                candidate = os.urandom(SESSION_LOCATOR_BYTES)
                if self._locator_is_free(endpoint_token, candidate):
                    return candidate
            raise SessionLocatorExhaustedError(
                "unable to generate a unique UDPSEC session locator after "
                f"{SESSION_LOCATOR_GENERATION_ATTEMPTS} attempts"
            )

    def _prepare_candidate_session(
        self,
        session_key,
        station_id,
        canonical_relation_key,
        current_epoch,
        active_path,
        now,
    ):
        """Acquire every fallible resource and fully construct a candidate
        `LogicalSession`, BEFORE any destructive mutation to existing
        state. Shared by `install_session` and `promote_pending_session`,
        whose only difference at this point is where `current_epoch`
        comes from (freshly built vs. transferred unchanged from a
        confirmed pending candidate).

        F1: an earlier version of this code could acquire/claim an
        `assembly_namespace`, THEN destroy the previous session at this
        relation (or evict a capacity victim), and only after that mint a
        `session_handle` -- so a handle-reservation failure left the
        namespace reservation orphaned and the previous session gone for
        nothing. Here, nothing destructive happens until the caller
        commits: on success, the returned `LogicalSession` fully owns a
        freshly-acquired `session_handle` and an `assembly_namespace`
        (freshly reserved, or one additional live reference claimed on a
        still-live previous session's namespace); on failure, exactly
        what THIS call newly acquired is released once and the exception
        propagates, with no other store touched.
        """
        assembly_namespace = self._reused_or_fresh_assembly_namespace(
            canonical_relation_key, station_id
        )
        try:
            session_handle = self._fresh_session_handle()
        except BaseException:
            _SESSION_IDENTITY_REGISTRY.release(assembly_namespace, now)
            raise
        try:
            return LogicalSession(
                _session_key=session_key,
                station_id=station_id,
                created_at=now,
                last_seen=now,
                session_handle=session_handle,
                assembly_namespace=assembly_namespace,
                current_epoch=current_epoch,
                path_state=PathState(active_path=active_path),
            )
        except BaseException:
            _SESSION_IDENTITY_REGISTRY.release(session_handle, now)
            _SESSION_IDENTITY_REGISTRY.release(assembly_namespace, now)
            raise

    def install_session(
        self,
        relation_key,
        station_id,
        session_locator,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now,
    ):
        relation_key = _require_endpoint_peer_key(relation_key)
        session_locator = _require_session_locator(session_locator)
        with self._lock:
            if self._closed:
                raise SecureStateClosedError(
                    "cannot install a session on a closed SecureState owner"
                )
            now = self._authoritative_now(now)
            self.cleanup_expired_sessions(now)

            # Preflight, before any destructive mutation: a locator already
            # identifying ANY live session or pending candidate on this
            # endpoint_token -- including this exact relation's own current
            # session -- fails closed here, leaving that relation's session
            # (and every other live session, the relation index, nonce
            # ledgers, and lifecycle statistics) untouched. Checking against
            # unmodified state also enforces that a same-relation replacement
            # can never reuse the outgoing session's own locator: V2 requires
            # a fresh server-minted locator for every whole-LogicalSession
            # replacement, so if it were the same locator this check would
            # (correctly) still see it occupied by the very session it would
            # otherwise replace.
            if not self._locator_is_free(
                relation_key.endpoint_token, session_locator
            ):
                raise SessionLocatorCollisionError(
                    "session_locator already identifies a different live "
                    "session on this endpoint_token"
                )

            # Canonicalize exactly once per transaction (can raise
            # `MalformedSockaddrError` for a malformed peer address; still
            # before any destructive mutation, so a raise here leaves
            # everything above untouched too) and reuse the result for
            # every relation-indexed step below, instead of each
            # independently re-canonicalizing the same unchanged input.
            canonical_relation_key = _canonical_active_relation_key(
                relation_key
            )
            session_key = _EndpointSessionKey(
                relation_key.endpoint_token, session_locator
            )

            # PREPARE (F1): every fallible acquisition and the fully
            # constructed candidate session happen here, before touching
            # the old session at this relation (if any) or a capacity
            # victim. See `_prepare_candidate_session`.
            session = self._prepare_candidate_session(
                session_key,
                station_id,
                canonical_relation_key,
                CryptoEpoch(
                    client_to_server_aesgcm=client_to_server_aesgcm,
                    server_to_client_aesgcm=server_to_client_aesgcm,
                    seen_data_nonces=_BoundedNonceSet(
                        self._data_nonce_max_per_session,
                    ),
                    created_at=now,
                ),
                relation_key.peer_address,
                now,
            )

            # COMMIT: the candidate is fully prepared, so only now remove
            # the old owner at this relation (if any) or evict a capacity
            # victim, and install the new session as one transaction.
            replaced = self._remove_active_at_canonical_relation_if_present(
                canonical_relation_key, now
            )
            if not replaced and len(self._sessions) >= self._max_sessions:
                oldest_session_key = next(iter(self._sessions))
                self._remove_session(oldest_session_key, "capacity", now)

            self._sessions[session_key] = session
            self._relation_index[canonical_relation_key] = session_key
            self._push_session_expiry(session)
            self._sessions_created += 1
            self._peak_sessions = max(
                self._peak_sessions,
                len(self._sessions),
            )
            return session

    def install_pending_session(
        self,
        relation_key,
        station_id,
        session_locator,
        client_to_server_aesgcm,
        server_to_client_aesgcm,
        now,
    ):
        relation_key = _require_endpoint_peer_key(relation_key)
        session_locator = _require_session_locator(session_locator)
        with self._lock:
            if self._closed:
                raise SecureStateClosedError(
                    "cannot install a pending session on a closed "
                    "SecureState owner"
                )
            now = self._authoritative_now(now)
            self.cleanup_expired_pending_sessions(now)

            # Preflight, before any destructive mutation (own-relation
            # replacement or capacity eviction): a locator already
            # identifying ANY live pending candidate or active session on
            # this endpoint_token -- including this exact relation's own
            # current pending candidate -- fails closed here, leaving that
            # relation's pending entry (and every other live pending entry,
            # its position in the capacity/eviction order, owner-index
            # entries, nonce ledgers, and lifecycle statistics) untouched.
            # Checking against unmodified state also enforces that a
            # same-relation pending replacement can never reuse the outgoing
            # candidate's own locator: each ServerHello mints its own fresh
            # locator, so if it were the same locator this check would
            # (correctly) still see it reserved by the very candidate it
            # would otherwise replace.
            if not self._locator_is_free(
                relation_key.endpoint_token, session_locator
            ):
                raise SessionLocatorCollisionError(
                    "session_locator already identifies a different live "
                    "session on this endpoint_token"
                )

            if relation_key in self._pending_sessions:
                self._remove_pending_session(relation_key, "replaced")
            elif len(self._pending_sessions) >= self._max_pending_sessions:
                oldest_relation_key = next(iter(self._pending_sessions))
                self._remove_pending_session(oldest_relation_key, "capacity")

            pending = _PendingSecureSession(
                _relation_key=relation_key,
                _address=relation_key.peer_address,
                station_id=station_id,
                session_locator=session_locator,
                created_at=now,
                current_epoch=CryptoEpoch(
                    client_to_server_aesgcm=client_to_server_aesgcm,
                    server_to_client_aesgcm=server_to_client_aesgcm,
                    seen_data_nonces=_BoundedNonceSet(
                        self._data_nonce_max_per_session,
                    ),
                    created_at=now,
                ),
            )
            self._pending_sessions[relation_key] = pending
            self._pending_locator_owners[
                (relation_key.endpoint_token, session_locator)
            ] = relation_key
            self._push_pending_expiry(pending)
            self._pending_sessions_created += 1
            self._peak_pending_sessions = max(
                self._peak_pending_sessions,
                len(self._pending_sessions),
            )
            return pending

    def get_active_session(self, session_key, now):
        session_key = _require_endpoint_session_key(session_key)
        with self._lock:
            self.cleanup_expired_sessions(now)
            return self._sessions.get(session_key)

    def get_pending_session(self, relation_key, now):
        relation_key = _require_endpoint_peer_key(relation_key)
        with self._lock:
            self.cleanup_expired_pending_sessions(now)
            return self._pending_sessions.get(relation_key)

    def _get_live_session_handle(self, session, now):
        session_key = session._session_key
        if self._sessions.get(session_key) is not session:
            return None

        self.cleanup_expired_sessions(now)
        if self._sessions.get(session_key) is not session:
            return None

        return session

    def _get_live_pending_session_handle(self, pending, now):
        relation_key = pending._relation_key
        if self._pending_sessions.get(relation_key) is not pending:
            return None

        self.cleanup_expired_pending_sessions(now)
        if self._pending_sessions.get(relation_key) is not pending:
            return None

        return pending

    def _touch_active_session(self, session, now):
        # Monotonic non-decreasing: two threads can race to touch the same
        # session with `now` values sampled before either acquired this
        # lock, so the one that actually commits second is not guaranteed
        # to carry the larger `now`. Clamping here means a stale, out-of-
        # order `now` can never move `last_seen` backwards and shorten
        # this session's real remaining lifetime -- `cleanup_expired_*`'s
        # lazy heap re-push (see `_push_session_expiry`) always recomputes
        # the deadline from this authoritative field, so a backwards jump
        # here would otherwise translate directly into a false-early
        # expiry decision.
        session.last_seen = max(session.last_seen, now)
        self._sessions.move_to_end(session._session_key)
        self._sessions_touched += 1

    def touch_session(self, session, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_session_handle(session, now) is None:
                return False
            self._touch_active_session(session, now)
            return True

    def is_live_session_handle(self, session, now):
        with self._lock:
            now = self._authoritative_now(now)
            return self._get_live_session_handle(session, now) is not None

    def close_session(self, session, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_session_handle(session, now) is None:
                return False
            self._remove_session(session._session_key, "closed", now)
            return True

    def close_pending_session(self, pending, now):
        """Discard the exact pending candidate `pending`, immediately
        rather than waiting for its ordinary TTL. F5: companion to
        `close_session` for the pending store -- exact-object-checked the
        same way, so a stale/already-superseded pending handle is
        correctly ignored rather than discarding whatever now legitimately
        occupies that relation. Pending candidates hold no registry
        identifier reservation of their own (`session_handle`/
        `assembly_namespace` are minted only by `install_session`/
        `promote_pending_session`), so there is nothing to release from
        `_SESSION_IDENTITY_REGISTRY` here."""
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_pending_session_handle(pending, now) is None:
                return False
            return self._remove_exact_pending_session(pending, "closed")

    def close(self, now):
        """R5/F5: explicit, deterministic OWNER-level teardown -- discard
        every active session and pending candidate THIS OWNER holds
        (releasing exactly this owner's `session_handle`/
        `assembly_namespace` reservations from the shared process-wide
        registry along the way, via the normal per-session/per-pending
        removal paths), and mark this owner closed.

        This is a wider, different operation from `close_owned_sessions`/
        `close_owned_pending_sessions`: those discard only the exact
        sessions/candidates ONE physical listener installed or promoted
        (its own `owned_sessions`/`owned_pending_sessions`), so one
        listener can stop without disturbing a SHARED owner other
        listeners still use. `close()` discards EVERYTHING this owner
        holds and ends this owner's own lifecycle -- call it only when
        this exact `SecureState` instance itself (not merely one listener
        sharing it) is being retired. It never touches any other
        `SecureState` owner, and never clears the shared identity
        registry -- other owners' live and retiring reservations are
        completely unaffected.

        Idempotent: closing an already-closed owner is a no-op, not an
        error. After this returns, `install_session()`/
        `install_pending_session()` on this owner raise `RuntimeError`
        rather than silently creating new state -- there is no reopen
        contract; construct a fresh `SecureState()` for a new owner.
        Existing exact-object-identity checks throughout this class
        already make every OTHER public method (touch, close, nonce
        admission, promotion) a safe no-op against state this call has
        already removed, so listener-level cleanup racing with (or
        running after) this call cannot double-release anything.

        Does not rely on `__del__`/garbage collection: this is the
        primary, explicit cleanup path, safe to call from any owner
        lifecycle a caller manages (a test, or a runtime's own final
        shutdown once every listener sharing this owner has already
        stopped).

        R6/F5: also discards this owner's handshake replay ledger and
        both expiry heaps -- a closed owner has no active/pending state
        left for either heap's entries to meaningfully describe, and
        `accept_handshake_replay` already refuses to admit anything new
        once closed (see that method), so retaining old replay records
        here would only be dead weight, not a live security boundary.
        Does not touch the shared `_SESSION_IDENTITY_REGISTRY`: every
        reservation this owner held was already released above, via the
        normal per-session/per-pending removal paths, exactly like any
        other removal.
        """
        with self._lock:
            if self._closed:
                return
            now = self._authoritative_now(now)
            for session_key in list(self._sessions.keys()):
                self._remove_session(session_key, "closed", now)
            for relation_key in list(self._pending_sessions.keys()):
                self._remove_pending_session(relation_key, "closed")
            self._handshake_replays.discard_all()
            self._session_expiry_heap.clear()
            self._pending_expiry_heap.clear()
            self._closed = True

    def promote_pending_session(self, pending, now):
        relation_key = pending._relation_key
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_pending_session_handle(pending, now) is None:
                return None

            self.cleanup_expired_sessions(now)

            # Preflight, before any destructive mutation: defense in depth
            # alongside install_session()'s and install_pending_session()'s
            # own checks. The pending's locator was already reserved (and
            # checked free) when the pending was installed, so an inconsistent
            # claim here should be unreachable in practice -- but promotion
            # writes `_sessions` directly rather than going through
            # install_session(), so the invariant is verified here too rather
            # than assumed. This is a pure query: it does not temporarily
            # release the pending's own reservation just to ask the question,
            # so a failure leaves the pending session, its locator
            # reservation, any current active session at this relation, and
            # every lifecycle statistic below untouched.
            if not self._pending_exclusively_owns_locator(
                relation_key, pending.session_locator
            ):
                raise SessionLocatorCollisionError(
                    "pending session_locator is not exclusively owned by "
                    "this pending candidate"
                )

            # Also before any destructive mutation: canonicalizing
            # `relation_key` (see `_canonical_active_relation_key`) can
            # raise `MalformedSockaddrError` for a malformed peer address.
            # Pending identity is exact-tuple-bound and is never itself
            # canonicalized at install time, so this is the first point
            # that shape is validated -- doing it here, before popping the
            # pending entry or releasing its locator reservation, ensures a
            # validation failure leaves the pending session, its
            # reservation, and every statistic below untouched, exactly
            # like the locator-ownership check above. The result is reused
            # for every relation-indexed step below (computed exactly once
            # per transaction), rather than each step independently
            # re-canonicalizing the same unchanged input.
            canonical_relation_key = _canonical_active_relation_key(
                relation_key
            )
            session_key = _EndpointSessionKey(
                relation_key.endpoint_token, pending.session_locator
            )

            # PREPARE (F1): every fallible acquisition and the fully
            # constructed candidate session happen here -- before popping
            # the pending entry, releasing its locator reservation,
            # touching the old active session at this relation (if any),
            # or evicting a capacity victim. See
            # `_prepare_candidate_session`. A failure here leaves the
            # pending session, its locator reservation, its confirmation-
            # nonce accounting, and every statistic below untouched: it
            # has not been popped yet, so nothing has become ownerless.
            session = self._prepare_candidate_session(
                session_key,
                pending.station_id,
                canonical_relation_key,
                # The confirmed epoch (both AES-GCM owners and the nonce
                # set that already admitted the confirmation nonce)
                # transfers unchanged from the pending candidate --
                # promotion does not derive new keys or reset replay
                # state. The pending's already-reserved session_locator
                # transfers unchanged too: promotion mints no second
                # locator.
                pending.current_epoch,
                relation_key.peer_address,
                now,
            )

            # COMMIT: the candidate is fully prepared, so only now pop the
            # pending entry, release its locator reservation, remove the
            # old active owner at this relation (if any) or evict a
            # capacity victim, and install the new session -- one
            # uncontested transaction.
            self._pending_sessions.pop(relation_key)
            locator_key = (relation_key.endpoint_token, pending.session_locator)
            if self._pending_locator_owners.get(locator_key) == relation_key:
                del self._pending_locator_owners[locator_key]
            self._pending_sessions_promoted += 1

            replaced = self._remove_active_at_canonical_relation_if_present(
                canonical_relation_key, now
            )
            if not replaced and len(self._sessions) >= self._max_sessions:
                oldest_session_key = next(iter(self._sessions))
                self._remove_session(oldest_session_key, "capacity", now)

            self._sessions[session_key] = session
            self._relation_index[canonical_relation_key] = session_key
            self._push_session_expiry(session)
            self._sessions_created += 1
            self._peak_sessions = max(
                self._peak_sessions,
                len(self._sessions),
            )
            return session

    def data_nonce_seen(self, session, nonce, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_session_handle(session, now) is None:
                return False
            seen = session.current_epoch.seen_data_nonces.contains(nonce)
            if seen:
                self._data_nonce_replays += 1
            return seen

    def pending_data_nonce_seen(self, pending, nonce, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_pending_session_handle(pending, now) is None:
                return False
            return pending.current_epoch.seen_data_nonces.contains(nonce)

    def _account_accepted_data_nonce(self):
        self._data_nonces_accepted += 1
        self._current_data_nonces += 1
        self._peak_data_nonces = max(
            self._peak_data_nonces,
            self._current_data_nonces,
        )

    def _remove_exact_session(self, session, reason, now):
        session_key = session._session_key
        if self._sessions.get(session_key) is not session:
            return False
        self._remove_session(session_key, reason, now)
        return True

    def _remove_exact_pending_session(self, pending, reason):
        relation_key = pending._relation_key
        if self._pending_sessions.get(relation_key) is not pending:
            return False
        self._remove_pending_session(relation_key, reason)
        return True

    def admit_data_nonce(self, session, nonce, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_session_handle(session, now) is None:
                return _DataNonceAdmission.STALE
            admission = session.current_epoch.seen_data_nonces.admit(nonce)
            if admission is _DataNonceAdmission.REPLAY:
                self._data_nonce_replays += 1
            elif admission is _DataNonceAdmission.EXHAUSTED:
                if not self._remove_exact_session(
                    session, "nonce_exhausted", now
                ):
                    return _DataNonceAdmission.STALE
            else:
                self._account_accepted_data_nonce()
            return admission

    def accept_data_nonce(self, session, nonce, now):
        with self._lock:
            now = self._authoritative_now(now)
            return (
                self.admit_data_nonce(session, nonce, now)
                is _DataNonceAdmission.ACCEPTED
            )

    def admit_pending_data_nonce(self, pending, nonce, now):
        with self._lock:
            now = self._authoritative_now(now)
            if self._get_live_pending_session_handle(
                pending, now
            ) is None:
                return _DataNonceAdmission.STALE
            admission = pending.current_epoch.seen_data_nonces.admit(nonce)
            if admission is _DataNonceAdmission.REPLAY:
                self._data_nonce_replays += 1
            elif admission is _DataNonceAdmission.EXHAUSTED:
                if not self._remove_exact_pending_session(
                    pending, "nonce_exhausted"
                ):
                    return _DataNonceAdmission.STALE
            else:
                self._account_accepted_data_nonce()
            return admission

    def accept_pending_data_nonce(self, pending, nonce, now):
        with self._lock:
            now = self._authoritative_now(now)
            return (
                self.admit_pending_data_nonce(pending, nonce, now)
                is _DataNonceAdmission.ACCEPTED
            )


# R5/F2: the process-wide production default is explicitly configured
# with the real authoritative clock (see `SecureState.__init__`'s `clock`
# parameter) -- a stale `now` sampled by any listener sharing this owner
# (before a delay, in a different thread, or held on an old asynchronous
# reference) can never make an already-expired session or pending
# candidate appear live. A `SecureState` a test constructs directly
# defaults to `clock=None` and is unaffected.
secure_state = SecureState(clock=time.monotonic)


def _update_replay_digest(digest, field):
    if not isinstance(field, bytes):
        raise TypeError("handshake replay fields must be bytes")
    if len(field) > (1 << 32) - 1:
        raise ValueError(
            "handshake replay field exceeds unsigned 32-bit framing"
        )
    digest.update(len(field).to_bytes(4, "big"))
    digest.update(field)


def build_handshake_replay_key(
    client_auth_digest,
    client_signature,
):
    if not isinstance(client_auth_digest, bytes):
        raise TypeError("client_auth_digest must be bytes")
    if len(client_auth_digest) != 32:
        raise ValueError("client_auth_digest must be exactly 32 bytes")
    if not isinstance(client_signature, bytes):
        raise TypeError("client_signature must be bytes")
    if not client_signature:
        raise ValueError("client_signature must not be empty")

    digest = hashes.Hash(hashes.SHA256())
    for field in (
        DOMAIN_CONTEXT,
        _HANDSHAKE_REPLAY_LABEL,
        client_auth_digest,
        client_signature,
    ):
        _update_replay_digest(digest, field)
    return digest.finalize()


def encrypt_secure_json_message(aesgcm, session_locator, message):
    """Encrypt one JSON DATA-channel message as one canonical V2 packet.

    Every direction/message kind (NMEA, ordinary ping/pong, confirmation
    ping/pong, graceful close) must use this exact construction so that
    changing the plaintext locator on a captured ciphertext invalidates
    AEAD authentication under the correct key.
    """

    nonce = os.urandom(12)
    plaintext = json.dumps(message, separators=(",", ":")).encode()
    ciphertext = aesgcm.encrypt(
        nonce, plaintext, build_data_aad(session_locator)
    )
    return build_data_packet(session_locator, nonce, ciphertext)


def close_owned_sessions(
    sock,
    state_owner,
    owned_sessions,
    *,
    wall_clock=None,
    monotonic_clock=None,
):
    """Best-effort close exact active sessions owned by one listener socket."""

    if not owned_sessions:
        return

    wall_now = time.time if wall_clock is None else wall_clock
    monotonic_now = (
        time.monotonic
        if monotonic_clock is None
        else monotonic_clock
    )
    for session_key, session in list(owned_sessions.items()):
        local_now = monotonic_now()
        if not state_owner.is_live_session_handle(session, local_now):
            continue
        addr = session.path_state.active_path
        try:
            message = build_session_close_message(
                session.station_id,
                int(wall_now()),
            )
            sock.sendto(
                encrypt_secure_json_message(
                    session.current_epoch.server_to_client_aesgcm,
                    session_key.session_locator,
                    message,
                ),
                addr,
            )
        except Exception as exc:
            print(
                f"[!] Best-effort secure close failed for "
                f"{format_endpoint_tuple(addr)}: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            state_owner.close_session(
                session,
                local_now,
            )


def close_owned_pending_sessions(
    state_owner,
    owned_pending_sessions,
    *,
    monotonic_clock=None,
):
    """F5: discard exact pending candidates owned by one listener socket
    immediately, rather than leaving them to expire on their own TTL.

    Companion to `close_owned_sessions` for the pending store. Unlike
    active sessions, an abandoned pending candidate holds no
    `_SESSION_IDENTITY_REGISTRY` reservation of its own to release
    (`session_handle`/`assembly_namespace` are minted only at
    `install_session`/`promote_pending_session` time) -- so there is
    nothing to leak here, but leaving a torn-down listener's own
    candidates to expire naturally would needlessly hold pending-capacity
    slots (and their locator reservations) that another owner sharing
    this `SecureState` could otherwise use in the meantime. No handshake
    message is sent: an unconfirmed candidate has no confirmed key
    material to notify with, and UDPSEC's client-side retry/timeout
    behavior on a silently-dropped pending handshake is already the
    ordinary, unremarkable path.
    """

    if not owned_pending_sessions:
        return

    monotonic_now = (
        time.monotonic
        if monotonic_clock is None
        else monotonic_clock
    )
    for relation_key, pending in list(owned_pending_sessions.items()):
        local_now = monotonic_now()
        state_owner.close_pending_session(pending, local_now)


def _build_server_handshake(
    client_hello,
    client_ephemeral_public_key,
    session_locator,
    *,
    server_private_key=None,
):
    """Build one authenticated ServerHello and directional session ciphers.

    `session_locator` must already be generated (and, by the time this
    response is actually installed, reserved) by the caller -- it is
    cryptographically bound into the server authentication digest and the
    session transcript hash below, so tampering with it invalidates both
    signature verification and the derived traffic keys.
    """

    active_server_private_key = _require_prepared_server_private_key(
        server_private_key
    )
    session_locator = _require_session_locator(session_locator)
    server_random = os.urandom(32)
    server_ephemeral_private_key = generate_ephemeral_private_key()
    server_ephemeral_public_bytes = serialize_ephemeral_public_key(
        server_ephemeral_private_key.public_key()
    )
    server_auth_digest = build_server_auth_digest(
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
    )
    server_signature = sign_transcript_digest(
        active_server_private_key,
        server_auth_digest,
    )
    shared_secret = derive_ephemeral_shared_secret(
        server_ephemeral_private_key,
        client_ephemeral_public_key,
    )
    session_transcript_hash = build_session_transcript_hash(
        protocol_version=client_hello.protocol_version,
        station_id=client_hello.station_id,
        timestamp=client_hello.timestamp,
        client_random=client_hello.client_random,
        client_ephemeral_public_key=(
            client_hello.client_ephemeral_public_key
        ),
        client_signature=client_hello.client_signature,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
        server_signature=server_signature,
    )
    key_material = derive_session_key_material(
        shared_secret,
        session_transcript_hash,
    )
    server_hello = ServerHello(
        protocol_version=UDPSEC_PROTOCOL_VERSION,
        session_locator=session_locator,
        server_random=server_random,
        server_ephemeral_public_key=server_ephemeral_public_bytes,
        server_signature=server_signature,
    )
    response_packet = build_server_hello_packet(server_hello)
    return (
        response_packet,
        AESGCM(key_material.client_to_server_key),
        AESGCM(key_material.server_to_client_key),
    )


async def _secure_server_loop(
    sock,
    queue,
    ip,
    port,
    sec_input_id=None,
    ingress_policy=None,
    *,
    endpoint_token,
    input_traffic=None,
    debug: bool = False,
    state=None,
    wall_clock=None,
    monotonic_clock=None,
    server_private_key=None,
    owned_sessions=None,
    owned_pending_sessions=None,
):
    active_server_private_key = _require_prepared_server_private_key(
        server_private_key
    )
    if not isinstance(endpoint_token, _EndpointToken):
        raise TypeError("endpoint_token must be an _EndpointToken")

    sock.bind((ip, port))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    policy = ingress_policy or NetworkPolicy.unrestricted()
    state_owner = secure_state if state is None else state
    wall_now = time.time if wall_clock is None else wall_clock
    monotonic_now = time.monotonic if monotonic_clock is None else monotonic_clock

    print(f"[+] Secure listener started on {format_endpoint(ip, port)}")

    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 8192)
        except (ConnectionResetError, ConnectionRefusedError) as e:
            # Recoverable peer/network receive condition (for example, an
            # ICMP port-unreachable response to an earlier reply surfacing as
            # WSAECONNRESET on the next Windows recv). This is not a broken
            # local socket or a runtime invariant failure, so the listener
            # stays up and simply awaits the next datagram. Any other OSError
            # is left to propagate to the essential-task supervisor.
            print(
                f"[!] Secure listener recv contained {type(e).__name__} "
                f"(peer/network condition): {e}"
            )
            continue
        if input_traffic is not None:
            input_traffic.transport_received(data)
        source_ip = addr[0]
        if not policy.allows(source_ip):
            continue
        relation_key = _EndpointPeerKey(endpoint_token, addr)
        local_now = monotonic_now()
        state_owner.cleanup_expired_sessions(local_now)
        state_owner.cleanup_expired_pending_sessions(local_now)

        if data.startswith(CLIENT_HELLO_PREFIX):
            try:
                client_hello = parse_client_hello_packet(data)
                station_id = client_hello.station_id
                timestamp = client_hello.timestamp

                if abs(wall_now() - timestamp) > 30:
                    print(
                        f"[!] Rejected {station_id}: timestamp out of window")
                    continue

                client_identity_public_key = AUTHORIZED_KEYS.get(station_id)
                if client_identity_public_key is None:
                    print(f"[!] Rejected {station_id}: unknown client")
                    continue

                client_auth_digest = build_client_auth_digest(
                    protocol_version=client_hello.protocol_version,
                    station_id=station_id,
                    timestamp=timestamp,
                    client_random=client_hello.client_random,
                    client_ephemeral_public_key=(
                        client_hello.client_ephemeral_public_key
                    ),
                )
                if not verify_transcript_signature(
                    client_identity_public_key,
                    client_hello.client_signature,
                    client_auth_digest,
                ):
                    raise ValueError(
                        "ClientHello identity signature verification failed"
                    )
                client_ephemeral_public_key = parse_ephemeral_public_key(
                    client_hello.client_ephemeral_public_key
                )
                replay_key = build_handshake_replay_key(
                    client_auth_digest,
                    client_hello.client_signature,
                )
                if not state_owner.accept_handshake_replay(
                    replay_key, local_now
                ):
                    print(f"[!] Rejected {station_id}: handshake replay")
                    continue

                # Minted before the ServerHello is signed and built: the
                # locator is cryptographically bound into the server
                # authentication digest and the session transcript hash.
                session_locator = state_owner.generate_session_locator(
                    endpoint_token
                )
                (
                    response_packet,
                    client_to_server_aesgcm,
                    server_to_client_aesgcm,
                ) = _build_server_handshake(
                    client_hello,
                    client_ephemeral_public_key,
                    session_locator,
                    server_private_key=active_server_private_key,
                )
                pending = state_owner.install_pending_session(
                    relation_key,
                    station_id,
                    session_locator,
                    client_to_server_aesgcm,
                    server_to_client_aesgcm,
                    local_now,
                )
                if owned_pending_sessions is not None:
                    owned_pending_sessions[relation_key] = pending

                sock.sendto(response_packet, addr)
                print(
                    f"[+] Sent authenticated ServerHello "
                    f"to {station_id} @ {format_endpoint_tuple(addr)}"
                )

            except Exception as e:
                print(
                    f"[!] Handshake error from {format_endpoint_tuple(addr)}: "
                    f"{type(e).__name__}: {e}")

        elif data.startswith(DATA_PREFIX):
            try:
                try:
                    packet_locator, nonce, ciphertext = parse_data_packet(
                        data
                    )
                except ValueError:
                    continue

                pending = state_owner.get_pending_session(
                    relation_key, local_now
                )
                if (
                    owned_pending_sessions is not None
                    and owned_pending_sessions.get(relation_key) is not pending
                ):
                    pending = None

                if pending is not None and packet_locator == (
                    pending.session_locator
                ):
                    # The locator identifies this packet as a pending-
                    # confirmation candidate for THIS exact pending relation
                    # -- process only as that, regardless of outcome. Never
                    # fall through to active lookup: a pending locator is
                    # guaranteed distinct from every currently active
                    # locator on this listener, so it could never match an
                    # active session anyway.
                    if state_owner.pending_data_nonce_seen(
                        pending, nonce, local_now
                    ):
                        print(
                            "[!] Duplicate secure data nonce from "
                            f"{format_endpoint_tuple(addr)}"
                        )
                        continue
                    try:
                        pending_plaintext = (
                            pending.current_epoch.client_to_server_aesgcm.decrypt(
                                nonce,
                                ciphertext,
                                build_data_aad(packet_locator),
                            )
                        )
                    except InvalidTag:
                        continue
                    pending_message = json.loads(
                        pending_plaintext.decode()
                    )
                    if not is_ping_message(
                        pending_message,
                        pending.station_id,
                        confirmation=True,
                    ):
                        print(
                            f"[!] Invalid session confirmation "
                            f"from {format_endpoint_tuple(addr)}"
                        )
                        continue
                    admission = state_owner.admit_pending_data_nonce(
                        pending, nonce, local_now
                    )
                    if admission is _DataNonceAdmission.REPLAY:
                        print(
                            f"[!] Duplicate secure data nonce "
                            f"from {format_endpoint_tuple(addr)}"
                        )
                        continue
                    if admission is _DataNonceAdmission.EXHAUSTED:
                        if (
                            owned_pending_sessions is not None
                            and owned_pending_sessions.get(
                                relation_key
                            ) is pending
                        ):
                            owned_pending_sessions.pop(
                                relation_key, None
                            )
                        print(
                            f"[!] Pending secure session nonce capacity "
                            f"exhausted for {format_endpoint_tuple(addr)}"
                        )
                        continue
                    if admission is not _DataNonceAdmission.ACCEPTED:
                        continue

                    session = state_owner.promote_pending_session(
                        pending,
                        local_now,
                    )
                    if session is None:
                        continue
                    if not state_owner.touch_session(session, local_now):
                        continue
                    if (
                        owned_pending_sessions is not None
                        and owned_pending_sessions.get(
                            relation_key
                        ) is pending
                    ):
                        owned_pending_sessions.pop(relation_key, None)
                    if owned_sessions is not None:
                        owned_sessions[session._session_key] = session

                    response = build_pong_message(
                        session.station_id,
                        SESSION_CONFIRMATION_SEQUENCE,
                        int(wall_now()),
                    )
                    # Pending-confirmation traffic is still tuple-bound:
                    # this reply belongs to the handshake transaction, so
                    # it addresses the confirming packet's own tuple, not
                    # (yet) `session.path_state.active_path`.
                    sock.sendto(
                        encrypt_secure_json_message(
                            session.current_epoch.server_to_client_aesgcm,
                            session._session_key.session_locator,
                            response,
                        ),
                        addr,
                    )
                    print(
                        f"[+] Confirmed secure session for "
                        f"{session.station_id} @ "
                        f"{format_endpoint_tuple(addr)}"
                    )
                    continue

                # Tuple-independent established lookup: the remote address
                # is no longer part of session identity.
                session = state_owner.get_active_session(
                    _EndpointSessionKey(endpoint_token, packet_locator),
                    local_now,
                )
                if session is None:
                    # Unknown locator: silent drop. No trial decryption
                    # against other sessions, no state created/touched, no
                    # reply -- and this reveals nothing about whether any
                    # station identity exists.
                    continue

                # No migration in this stage: a locator match from any path
                # other than the session's currently validated active_path
                # is dropped before any cryptographic work, exactly like an
                # unknown locator. A later stage will turn this into
                # authenticated candidate-path evaluation.
                if not _structured_paths_match(
                    addr, session.path_state.active_path
                ):
                    continue

                station_id = session.station_id
                client_to_server_aesgcm = (
                    session.current_epoch.client_to_server_aesgcm
                )
                if state_owner.data_nonce_seen(session, nonce, local_now):
                    print(
                        "[!] Duplicate secure data nonce from "
                        f"{format_endpoint_tuple(addr)}"
                    )
                    continue

                plaintext = client_to_server_aesgcm.decrypt(
                    nonce,
                    ciphertext,
                    build_data_aad(packet_locator),
                )

                msg = json.loads(plaintext.decode())
                if msg.get("source_id") != station_id:
                    print(
                        f"[!] source_id mismatch from "
                        f"{format_endpoint_tuple(addr)}"
                    )
                    continue

                message_type = msg.get("type")
                if message_type == "ping":
                    if not is_ping_message(msg, station_id):
                        print(
                            f"[!] Invalid ping from "
                            f"{format_endpoint_tuple(addr)}"
                        )
                        continue
                elif message_type == "nmea":
                    if not isinstance(msg.get("payload"), str):
                        print(
                            f"[!] Invalid NMEA data from "
                            f"{format_endpoint_tuple(addr)}"
                        )
                        continue
                elif message_type == SESSION_CLOSE_TYPE:
                    if not is_session_close_message(msg, station_id):
                        print(
                            f"[!] Invalid session close from "
                            f"{format_endpoint_tuple(addr)}"
                        )
                        continue
                    if (
                        owned_sessions is not None
                        and owned_sessions.get(
                            session._session_key
                        ) is not session
                    ):
                        continue
                else:
                    print(
                        f"[!] Unknown secure message type from "
                        f"{format_endpoint_tuple(addr)}"
                    )
                    continue

                admission = state_owner.admit_data_nonce(
                    session, nonce, local_now
                )
                if admission is _DataNonceAdmission.REPLAY:
                    print(
                        "[!] Duplicate secure data nonce from "
                        f"{format_endpoint_tuple(addr)}"
                    )
                    continue
                if admission is _DataNonceAdmission.EXHAUSTED:
                    if (
                        owned_sessions is not None
                        and owned_sessions.get(
                            session._session_key
                        ) is session
                    ):
                        owned_sessions.pop(session._session_key, None)
                    print(
                        f"[!] Secure session nonce capacity exhausted "
                        f"for {format_endpoint_tuple(addr)}"
                    )
                    continue
                if admission is not _DataNonceAdmission.ACCEPTED:
                    continue

                if message_type == SESSION_CLOSE_TYPE:
                    state_owner.close_session(
                        session,
                        local_now,
                    )
                    continue

                state_owner.touch_session(
                    session,
                    local_now,
                )

                if message_type == "ping":
                    response = build_pong_message(
                        station_id,
                        msg["seq"],
                        int(wall_now()),
                    )
                    # Established-session traffic addresses the session's
                    # authoritative outbound path, not necessarily the tuple
                    # this particular packet happened to arrive from. In this
                    # stage there is no migration yet, so `active_path` is
                    # always the same value `addr` already has -- but this is
                    # now the one place later path-validation work needs to
                    # change.
                    sock.sendto(
                        encrypt_secure_json_message(
                            session.current_epoch.server_to_client_aesgcm,
                            session._session_key.session_locator,
                            response,
                        ),
                        session.path_state.active_path,
                    )
                    continue

                src_for_queue = sec_input_id or station_id or "ANONYMOUS"
                peer = addr if 'addr' in locals() else None
                remote_ip = peer[0] if isinstance(
                    peer, tuple) and peer else None
                # assembly_namespace, not session_handle and not a rendered
                # remote tuple: two fragments of one multipart message must
                # group together even across a same-station same-relation
                # rekey (or, later, a real path migration), while a
                # different authenticated station or a genuinely different
                # LogicalSession must never share a namespace.
                assembler_key = (
                    f"udpsec-assembly:{session.assembly_namespace.hex()}"
                )
                # R6/F4: an explicit, frame-scoped hold on this exact
                # assembly_namespace reservation, taken out now -- while
                # `session` is known live, because `touch_session` above
                # just confirmed it, with no `await` in between -- so
                # THIS frame's own right to eventually reach the
                # assembler survives even an arbitrary pause anywhere
                # downstream, independent of the owning session's own
                # remaining lifetime. See `core.session_identity_registry.
                # SessionIdentityRegistry.lease` and
                # `IngressFrame.admission_lease`/`release_admission_lease`
                # for the release side of this, and
                # `core.python_data_plane` for why this -- not the age
                # check below -- is what actually closes the race a
                # one-time age observation cannot.
                try:
                    namespace_lease = _SESSION_IDENTITY_REGISTRY.lease(
                        session.assembly_namespace
                    )
                except ValueError:
                    # The session's own reference on this namespace was
                    # released concurrently, between `touch_session`
                    # above and here -- only possible if another thread
                    # sharing this exact SecureState removed this exact
                    # session in that instant. There is no live
                    # reservation left to protect, so this data message
                    # is dropped exactly like any other lost race against
                    # session removal, rather than raising out of this
                    # handler.
                    continue
                frame = frame_from_text_payload(
                    kind="sec",
                    source_id=build_udpsec_source_id(station_id),
                    alias_for_s=src_for_queue,
                    remote_ip=remote_ip,
                    assembler_key=assembler_key,
                    payload=msg["payload"],
                    # F4: this frame's assembler_key is derived from
                    # assembly_namespace, which can legitimately be
                    # reserved for an unrelated station once its
                    # retirement window elapses. Recording admission time
                    # here lets the processor (core.python_data_plane)
                    # apply its own bounded-backlog/QoS policy, on top of
                    # (not instead of) the lease above.
                    admitted_at=local_now,
                    admission_lease=namespace_lease,
                )
                if frame is None:
                    # Unreachable in practice (payload was already
                    # validated as a str above), but defensively release
                    # rather than leak if it ever is.
                    namespace_lease.release(local_now)
                else:
                    try:
                        await queue.put(frame)
                    except BaseException:
                        # The frame never actually entered the pipeline
                        # (queue admission failed, or this task was
                        # cancelled while awaiting capacity) -- ownership
                        # of the lease never transferred anywhere else,
                        # so it must be released here, not leaked.
                        namespace_lease.release(local_now)
                        raise
                    if input_traffic is not None:
                        input_traffic.frame_accepted(frame.payload)

                if debug:
                    print(
                        f"{wall_now()} [SECURE] "
                        f"From {station_id}: {msg['payload']}")

            except Exception as e:
                print(
                    f"[!] Secure data error from {format_endpoint_tuple(addr)}: "
                    f"{type(e).__name__}: {e}")


async def secure_server(
    queue,
    ip,
    port,
    sec_input_id=None,
    ingress_policy=None,
    *,
    input_traffic=None,
    debug: bool = False,
    state=None,
    wall_clock=None,
    monotonic_clock=None,
    server_private_key=None,
):
    """Run one secure ingress producer and close its owned socket exactly once."""

    sock = create_udp_listener_socket(ip, reuse_address=False)
    endpoint_token = _new_endpoint_token()
    state_owner = secure_state if state is None else state
    # SecureState may be shared by multiple listeners. The opaque token scopes
    # every peer relation to this exact socket incarnation. Weak exact handles
    # retain complementary confirmation and shutdown ownership without keeping
    # removed state alive.
    owned_sessions = weakref.WeakValueDictionary()
    owned_pending_sessions = weakref.WeakValueDictionary()
    try:
        await _secure_server_loop(
            sock,
            queue,
            ip,
            port,
            sec_input_id=sec_input_id,
            ingress_policy=ingress_policy,
            endpoint_token=endpoint_token,
            input_traffic=input_traffic,
            debug=debug,
            state=state_owner,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
            server_private_key=server_private_key,
            owned_sessions=owned_sessions,
            owned_pending_sessions=owned_pending_sessions,
        )
    finally:
        try:
            close_owned_sessions(
                sock,
                state_owner,
                owned_sessions,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            )
        finally:
            try:
                close_owned_pending_sessions(
                    state_owner,
                    owned_pending_sessions,
                    monotonic_clock=monotonic_clock,
                )
            finally:
                sock.close()
