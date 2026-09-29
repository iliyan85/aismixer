"""Shared evidence and assertion helpers for the MP0 recovery tests.

Assertions here state OUTCOMES observable at the protocol boundary (client
sessions, wire datagrams, server `SecureState` counters and object
identity). They deliberately do not name any future MP1 class, retry count
or timing constant.
"""

from .lab import CLIENT_SOCKET_TIMEOUT, STILL_RUNNING, T0

# Authenticated server->client messages that current code accepts as peer
# liveness evidence when they match (BEHAVIORAL_CONTRACT.md section 11):
# a matching PONG, the first verified REFRESH_REPLY, a genuine REFRESH_ACK
# and a proof-matched PATH_ACK.
LIVENESS_EVIDENCE_KINDS = ("pong", "refresh_reply", "refresh_ack", "path_ack")
PROMPT_DELIVERY_SECONDS = 0.5


def keepalive_deadline(lab, seq, session_index=0):
    """`last_ping_at + keepalive_interval` for ping#`seq`, computed exactly
    as `forward_loop()` computes it."""
    session = lab.sessions[session_index]
    ping = lab.first_sent("c2s", f"ping#{seq}", locator=session.locator)
    return ping.t + float(lab.config["keepalive_interval"])


def last_evidence_before(lab, before, session_index=0):
    """Latest instant before `before` at which `forward_loop()` read an
    authenticated liveness datagram for this session (or the session
    start). Only used on scenarios whose pre-fault traffic is all genuine,
    where every such read is an accepted piece of evidence."""
    session = lab.sessions[session_index]
    times = [session.confirmed_at]
    for packet in lab.sent("s2c", locator=session.locator):
        if packet.consumed_by != "forward_loop" or packet.consumed_at is None:
            continue
        if packet.kind == "pong#0" or packet.consumed_at >= before:
            continue
        if packet.kind.split("#")[0] in LIVENESS_EVIDENCE_KINDS:
            times.append(packet.consumed_at)
    return max(times)


def require_window_proves_regained_liveness(lab, fault_time):
    """A session still running at scenario end only proves it regained
    authenticated liveness if the window outlasts the fault by a keepalive
    interval plus `peer_timeout`."""
    needed = (
        fault_time
        + float(lab.config["keepalive_interval"])
        + float(lab.config["peer_timeout"])
    )
    lab.require(
        lab.until >= needed,
        f"window ends at {lab.until}, before {needed}: 'still running' "
        "would not prove regained liveness",
    )


def assert_logical_session_survived(lab):
    """Exactly one client session for the whole window, no ClientHello
    after the first, and the first server `LogicalSession` object is still
    the live one (hence same locator, replay ledger ownership and assembly
    namespace)."""
    reasons = [session.reason for session in lab.sessions]
    assert reasons == [STILL_RUNNING], (
        f"{lab.name}: logical session not preserved; client sessions "
        f"ended with {reasons}"
    )
    assert lab.hello_times() == [T0], (
        f"{lab.name}: fresh establishment attempted at {lab.hello_times()}"
    )
    stats = lab.state.stats()
    assert (stats.sessions_created, stats.sessions_replaced) == (1, 0), (
        f"{lab.name}: server created {stats.sessions_created} and replaced "
        f"{stats.sessions_replaced} sessions"
    )
    original = lab.original_server_session()
    assert original is not None
    assert lab.server_session(lab.sessions[0].locator) is original


def lines_due_between(lab, start, end):
    return [
        (marker, due_at, read_at)
        for marker, due_at, read_at in lab.input.produced
        if start <= due_at < end
    ]


def assert_delivered_promptly(lab, lines, max_latency=PROMPT_DELIVERY_SECONDS):
    late = []
    for marker, due_at, _read_at in lines:
        delivered = lab.delivered_at(marker)
        if delivered is None or delivered - due_at > max_latency:
            late.append((marker, due_at, delivered))
    assert not late, f"{lab.name}: lines not delivered promptly: {late}"


def assert_no_nonce_reuse(lab):
    duplicates = lab.nonce_reuse()
    assert not duplicates, (
        f"{lab.name}: AEAD nonce reused: "
        + ", ".join(f"{a.kind}@{a.t}/{b.kind}@{b.t}" for a, b in duplicates)
    )


def assert_bounded_retry_cadence(lab):
    """No busy re-establishment loop: consecutive ClientHello attempts are
    at least `reconnect_delay` apart, except the single immediate attempt
    that follows a session end whose reason `retry_delay_for_reason()`
    classifies as immediate."""
    immediate_ends = [
        session.ended_at
        for session in lab.sessions
        if session.reason not in (None, STILL_RUNNING)
        and lab.proxy.retry_delay_for_reason(session.reason, lab.config) is None
    ]
    delay = float(lab.config["reconnect_delay"])
    attempts = lab.hello_attempt_times()
    for previous, current in zip(attempts, attempts[1:]):
        if any(current == ended for ended in immediate_ends):
            continue
        assert current - previous >= delay, (
            f"{lab.name}: ClientHello at {current} only {current - previous}s "
            f"after the previous one (reconnect_delay {delay})"
        )


def assert_bounded_server_state(lab):
    stats = lab.state.stats()
    assert stats.current_candidate_paths <= stats.current_sessions
    assert stats.current_retired_paths <= stats.current_sessions
    assert stats.current_pending_sessions <= 1
    assert stats.current_pending_epochs <= stats.current_sessions
    assert stats.current_retiring_epochs <= stats.current_sessions


def fresh_session_bound_after(lab, restored_at):
    """Latest acceptable confirmation time for a fresh session once the
    path is usable again at `restored_at`: one in-flight handshake timeout,
    one `reconnect_delay`, and a round trip."""
    return restored_at + CLIENT_SOCKET_TIMEOUT + float(lab.config["reconnect_delay"]) + 1.0
