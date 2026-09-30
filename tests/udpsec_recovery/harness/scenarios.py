"""MP0 recovery scenario catalogue.

Each builder runs one deterministic scenario in a fresh `Lab` and returns
it finished. IDs follow SCENARIO_MATRIX.md; the original Fable harness ID
(`T01`...`T16`) is kept wherever the scenario came from it, with the same
fault injection and timings. Scenarios marked "MP0" were added for this
baseline. Windows are at least as long as Fable's, and long enough that a
session which never regained authenticated liveness after its fault would
have reached `peer_timeout` before the window ends.

Fake time starts at T0 = 1000.0; the first session is confirmed at
1000.2 (2 RTT at 50 ms one-way), so ordinary keepalive pings leave at
1030.2, 1060.2, ... and ping#2 is the first one each fault targets.
"""

import errno
import weakref
from collections import namedtuple

from .lab import (
    ADDR_A,
    ADDR_A2,
    ADDR_B,
    ADDR_C,
    CGNAT_AFTER,
    CGNAT_BEFORE,
    T0,
    Lab,
    nmea_sentence,
)

Scenario = namedtuple("Scenario", "mp0_id fable_id recovery_class title build")


# --------------------------------------------------------------- fault hooks


def drop_once(kind, seq=None):
    """Drop the first `kind` message (optionally only sequence `seq`)."""
    state = {"done": False}

    def hook(message, t, lab):
        if state["done"] or not message or message.get("type") != kind:
            return False
        if seq is not None and message.get("seq") != seq:
            return False
        state["done"] = True
        lab.log("fault", f"dropped {kind}" + ("" if seq is None else f"#{seq}"))
        return True

    return hook


def drop_every(kind, min_seq=0):
    """Drop every `kind` message whose sequence is at least `min_seq`."""

    def hook(message, t, lab):
        if not message or message.get("type") != kind:
            return False
        if message.get("seq", 0) < min_seq:
            return False
        lab.log("fault", f"dropped {kind}#{message.get('seq')}")
        return True

    return hook


def drop_challenges_of_generation(generation):
    """Drop every PATH_CHALLENGE -- the initial send and each MP2 retry --
    of the one candidate incarnation with this `path_generation`."""

    def hook(message, t, lab):
        if not message or message.get("type") != "path_challenge":
            return False
        if message.get("path_generation") != generation:
            return False
        lab.log("fault", f"dropped path_challenge generation {generation}")
        return True

    return hook


def hold_once(kind, seconds, seq=None):
    """Delay the first `kind` message by `seconds` extra."""
    state = {"done": False}

    def hook(message, t, lab):
        if state["done"] or not message or message.get("type") != kind:
            return 0.0
        if seq is not None and message.get("seq") != seq:
            return 0.0
        state["done"] = True
        lab.log("fault", f"held {kind} by {seconds}s")
        return seconds

    return hook


def deliver_first_pong_at(seq, when):
    """Deliver the first server pong#`seq` at absolute time `when(lab)`."""
    state = {"done": False}

    def hook(message, t, lab):
        if state["done"] or not message or message.get("type") != "pong":
            return None
        if message.get("seq") != seq:
            return None
        state["done"] = True
        deliver_at = when(lab)
        lab.log("fault", f"pong#{seq} held until {deliver_at!r}")
        return deliver_at

    return hook


def _keepalive_deadline_of_ping2(lab):
    """The client's exact keepalive deadline for ping#2 (`last_ping_at +
    keepalive_interval`, computed exactly as `forward_loop` does)."""
    return lab.first_sent("c2s", "ping#2").t + float(lab.config["keepalive_interval"])


# --------------------------------------------------------------- A: same tuple


def a1_healthy(monkeypatch):
    """Fable T01: healthy network, same tuple, >= 6 keepalive cycles."""
    return Lab("A1_T01_healthy", monkeypatch).run_client(until=T0 + 200.0)


def a2_lost_ping(monkeypatch):
    """Fable T02: drop exactly one client PING (ping#2, sent at 1060.2)."""
    lab = Lab("A2_T02_lost_ping", monkeypatch)
    lab.drop_c2s = drop_once("ping", 2)
    return lab.run_client(until=T0 + 200.0)


def a3_lost_pong(monkeypatch):
    """Fable T03: deliver ping#2, drop exactly its PONG."""
    lab = Lab("A3_T03_lost_pong", monkeypatch)
    lab.drop_s2c = drop_once("pong", 2)
    return lab.run_client(until=T0 + 200.0)


def a4_short_blackhole(monkeypatch):
    """Fable T04: bidirectional blackhole 1058-1063 around ping#2; the path
    is healthy again 27 s before the next keepalive deadline."""
    lab = Lab("A4_T04_blackhole_5s", monkeypatch)
    lab.blackhole(1058.0, 1063.0)
    return lab.run_client(until=T0 + 200.0)


def a4b_blackhole_past_deadline(monkeypatch):
    """Fable T04b: bidirectional blackhole 1058-1095, outlasting the
    keepalive deadline (1090.2) by 4.8 s."""
    lab = Lab("A4b_T04b_blackhole_37s", monkeypatch)
    lab.blackhole(1058.0, 1095.0)
    return lab.run_client(until=T0 + 200.0)


def _pong2_relative_to_keepalive_deadline(name, monkeypatch, offset):
    lab = Lab(name, monkeypatch)
    lab.deliver_s2c_at = deliver_first_pong_at(
        2, lambda lab_: _keepalive_deadline_of_ping2(lab_) + offset
    )
    return lab.run_client(until=T0 + 200.0)


def a5a_pong_before_deadline(monkeypatch):
    """Fable T05a: pong#2 arrives 10 ms before its keepalive deadline."""
    return _pong2_relative_to_keepalive_deadline(
        "A5a_T05a_pong_before_deadline", monkeypatch, -0.01
    )


def a5b_pong_at_deadline(monkeypatch):
    """Fable T05b: pong#2 becomes readable exactly at its keepalive deadline."""
    return _pong2_relative_to_keepalive_deadline(
        "A5b_T05b_pong_at_deadline", monkeypatch, 0.0
    )


def a5c_pong_after_deadline(monkeypatch):
    """Fable T05c: pong#2 arrives 10 ms after its keepalive deadline."""
    return _pong2_relative_to_keepalive_deadline(
        "A5c_T05c_pong_after_deadline", monkeypatch, +0.01
    )


def a5d_pong_at_peer_timeout_boundary(monkeypatch):
    """MP0: with `peer_timeout: 45` (valid config), pong#1 becomes readable
    exactly at the peer_timeout boundary (session start + 45 s) -- the only
    terminal deadline due at that instant. Isolates the ordering of
    "authenticated evidence already readable" vs "terminal verdict" from
    any retry policy."""
    lab = Lab("A5d_MP0_pong_at_peer_timeout_boundary", monkeypatch)
    lab.deliver_s2c_at = deliver_first_pong_at(
        1,
        lambda lab_: lab_.sessions[0].confirmed_at
        + float(lab_.config["peer_timeout"]),
    )
    return lab.run_client(until=T0 + 200.0, peer_timeout=45)


def a5e_pong_after_peer_timeout_boundary(monkeypatch):
    """MP1: as A5d, but pong#1 becomes readable 10 ms AFTER the
    peer_timeout boundary. Evidence before verdict only admits what is
    already available when the verdict falls due; this pong is late."""
    lab = Lab("A5e_MP1_pong_after_peer_timeout_boundary", monkeypatch)
    lab.deliver_s2c_at = deliver_first_pong_at(
        1,
        lambda lab_: lab_.sessions[0].confirmed_at
        + float(lab_.config["peer_timeout"])
        + 0.01,
    )
    return lab.run_client(until=T0 + 200.0, peer_timeout=45)


def a6_client_stall(monkeypatch):
    """Fable T07: pong#2 is delivered normally (1060.30) but the client
    process does not run again until 1095.0 (I/O stall, SIGSTOP, ...); the
    pong sits in the socket buffer across the keepalive deadline."""
    lab = Lab("A6_T07_client_stall_35s", monkeypatch)

    def stall(lab_):
        lab_.log("fault", "client process descheduled until 1095.0")
        lab_.clock.now = 1095.0

    lab.at(1060.26, stall)
    return lab.run_client(until=T0 + 200.0)


def a7_reverse_path_loss(monkeypatch):
    """Fable T06a: server->client lost 1055-1155 while client->server stays
    healthy."""
    lab = Lab("A7_T06a_reverse_path_loss_100s", monkeypatch)
    lab.blackhole(1055.0, 1155.0, "s2c")
    return lab.run_client(until=T0 + 260.0)


def a9_refresh_evidence_with_lost_pong(monkeypatch):
    """Fable T14: pong#2 lost; a planned in-session epoch refresh commits
    at ~1065.4 (authenticated REPLY/ACK evidence) before the deadline."""
    lab = Lab("A9_T14_refresh_evidence_lost_pong", monkeypatch)
    lab.drop_s2c = drop_once("pong", 2)
    return lab.run_client(until=T0 + 200.0, session_refresh_interval=65)


def a10_ping_overtakes_path_response(monkeypatch):
    """Fable T09d (also B5): tuple A->B at 1059.9; the PATH_RESPONSE (which
    captured no outstanding ping) is held 0.4 s, so ping#2 (1060.2)
    overtakes it and reaches the server from the still-unproved candidate."""
    lab = Lab("A10_T09d_ping_overtakes_path_response", monkeypatch)
    lab.remap(1059.9, ADDR_B)
    lab.hold_c2s = hold_once("path_response", 0.4)
    return lab.run_client(until=T0 + 200.0)


def a9b_refresh_only_evidence(monkeypatch):
    """MP1 (L7-strong, baseline 7.3): every PONG from pong#2 on is lost;
    planned in-session refreshes (65 s) are the only authenticated
    liveness evidence after pong#1 (1030.3)."""
    lab = Lab("A9b_MP1_refresh_only_evidence", monkeypatch)
    lab.drop_s2c = drop_every("pong", min_seq=2)
    return lab.run_client(until=T0 + 300.0, session_refresh_interval=65)


def a10b_path_ack_only_evidence(monkeypatch):
    """MP1 (L8-strong, baseline 7.3): as A10, but every PONG from pong#2
    on is lost too, so the proof-matched PATH_ACK (1060.6) is the last
    authenticated liveness evidence -- and it can never resolve ping#2."""
    lab = Lab("A10b_MP1_path_ack_only_evidence", monkeypatch)
    lab.remap(1059.9, ADDR_B)
    lab.hold_c2s = hold_once("path_response", 0.4)
    lab.drop_s2c = drop_every("pong", min_seq=2)
    return lab.run_client(until=T0 + 250.0)


def a11_short_local_send_error(monkeypatch):
    """MP0: a 2.1 s local interface flap (sendto raises ENETUNREACH,
    1059.9-1062.0) on an otherwise healthy path."""
    lab = Lab("A11_MP0_short_local_send_error", monkeypatch)
    lab.local_send_error(1059.9, 1062.0, errno.ENETUNREACH)
    lab.blackhole(1059.9, 1062.0, "s2c")
    return lab.run_client(until=T0 + 200.0)


def a11b_transient_receive_error(monkeypatch):
    """MP1: at 1070 the client socket reports one ICMP-derived reset on
    recvfrom (Windows behaviour after a port-unreachable) on an otherwise
    healthy path."""
    lab = Lab("A11b_MP1_transient_receive_error", monkeypatch)
    lab.receive_error_at(1070.0)
    return lab.run_client(until=T0 + 200.0)


# --------------------------------------------------------------- B: tuple change


def b1_migration(monkeypatch):
    """Fable T08: clean A->B remap at 1045; NMEA from B opens the candidate."""
    lab = Lab("B1_T08_migration_a_to_b", monkeypatch)
    lab.snapshot_at(1044.0, "before")
    lab.remap(1045.0, ADDR_B)
    return lab.run_client(until=T0 + 200.0)


def _lost_control(name, monkeypatch, kind, nmea_interval=10.0):
    lab = Lab(name, monkeypatch, nmea_interval=nmea_interval)
    lab.remap(1045.0, ADDR_B)
    if kind == "path_response":
        lab.drop_c2s = drop_once(kind)
    else:
        lab.drop_s2c = drop_once(kind)
    return lab.run_client(until=T0 + 200.0)


def b2_lost_challenge(monkeypatch):
    """Fable T09a: first PATH_CHALLENGE lost (10 s NMEA cadence)."""
    return _lost_control("B2_T09a_lost_challenge", monkeypatch, "path_challenge")


def b2b_lost_challenge_7s(monkeypatch):
    """Fable T09a7: first PATH_CHALLENGE lost, 7 s NMEA cadence."""
    return _lost_control(
        "B2b_T09a7_lost_challenge_nmea7s", monkeypatch, "path_challenge", 7.0
    )


def b3_lost_response(monkeypatch):
    """Fable T09b: first PATH_RESPONSE lost."""
    return _lost_control("B3_T09b_lost_response", monkeypatch, "path_response")


def b4_lost_ack(monkeypatch):
    """Fable T09c: first PATH_ACK lost."""
    return _lost_control("B4_T09c_lost_ack", monkeypatch, "path_ack")


def b6a_refresh_starts_while_candidate_open(monkeypatch):
    """Fable T11b: candidate opened at ~1050.05 under E0 (challenge held
    1.5 s); planned refresh (50 s) starts while it is still unproved."""
    lab = Lab("B6a_T11b_refresh_while_candidate_open", monkeypatch)
    lab.remap(1045.0, ADDR_B)
    lab.hold_s2c = hold_once("path_challenge", 1.5)
    return lab.run_client(until=T0 + 200.0, session_refresh_interval=50)


def b6b_stale_candidate_after_refresh(monkeypatch):
    """Fable T11c: a 0.2 s flap to B opens an E0-bound candidate whose
    challenge is unreachable; refresh commits E1; later B traffic must
    replace the stale candidate with a fresh E1 incarnation."""
    lab = Lab("B6b_T11c_stale_e0_candidate_after_refresh", monkeypatch)
    lab.remap(1049.9, ADDR_B)
    lab.remap(1050.1, ADDR_A)
    lab.remap(1052.0, ADDR_B)
    return lab.run_client(until=T0 + 200.0, session_refresh_interval=50)


def b7_refresh_then_migration(monkeypatch):
    """Fable T11a: refresh (40 s) commits at ~1040.4; A->B at 1045 runs
    under E1."""
    lab = Lab("B7_T11a_refresh_then_migration", monkeypatch)
    lab.remap(1045.0, ADDR_B)
    return lab.run_client(until=T0 + 200.0, session_refresh_interval=40)


def b8_rapid_a_b_c(monkeypatch):
    """Fable T10b: A->B (1045) then B->C (1052), plus one old-path NMEA
    line from A held 6 s so it arrives after the A->B commit."""
    lab = Lab("B8_T10b_a_b_c_late_old_path", monkeypatch)
    lab.remap(1045.0, ADDR_B)

    def late_line(lab_):
        lab_.input.extra.append(
            (nmea_sentence("AIVDM,1,1,,A,LATE-A,0") + "\r\n").encode()
        )

    lab.at(1044.5, late_line)
    held = {"done": False}

    def hold_old_path_line(message, t, lab_):
        if held["done"] or not message or message.get("type") != "nmea":
            return 0.0
        if not (1044.4 <= t < 1045.0):
            return 0.0
        held["done"] = True
        lab_.log("fault", "held old-path nmea by 6s")
        return 6.0

    lab.hold_c2s = hold_old_path_line
    lab.remap(1052.0, ADDR_C)
    return lab.run_client(until=T0 + 200.0)


def b10_flap_during_challenge(monkeypatch):
    """Fable T10a: A->B at 1045, challenge held 3 s, back to A at 1052."""
    lab = Lab("B10_T10a_flap_a_b_a", monkeypatch)
    lab.remap(1045.0, ADDR_B)
    lab.hold_s2c = hold_once("path_challenge", 3.0)
    lab.remap(1052.0, ADDR_A)
    return lab.run_client(until=T0 + 200.0)


def b11_canonical_sockaddr(monkeypatch):
    """Fable T12: flowinfo-only change (A->A2) is not a path change; a
    family/port change (->IPv4 C) is."""
    lab = Lab("B11_T12_canonical_sockaddr", monkeypatch)
    lab.remap(1045.0, ADDR_A2)
    lab.remap(1075.0, ADDR_C)
    return lab.run_client(until=T0 + 200.0)


def b12_every_attempt_of_one_incarnation_lost(monkeypatch):
    """MP2: A->B at 1045; every PATH_CHALLENGE of the first candidate
    incarnation -- its initial send and all of its retries -- is lost. That
    incarnation spends its whole budget and expires at its unchanged
    deadline; later authenticated traffic from B opens a new incarnation,
    which commits."""
    lab = Lab("B12_MP2_all_attempts_of_one_incarnation_lost", monkeypatch)
    lab.remap(1045.0, ADDR_B)
    lab.drop_s2c = drop_challenges_of_generation(1)
    return lab.run_client(until=T0 + 200.0)


# --------------------------------------------------------------- C: terminal


def c1_long_local_network_loss(monkeypatch):
    """MP0 (field-derived): local Wi-Fi lost for 6 min -- sendto raises
    ENETUNREACH and nothing arrives, 1058-1418 -- same public tuple."""
    lab = Lab("C1_MP0_long_local_wifi_loss", monkeypatch)
    lab.external_addr = CGNAT_BEFORE
    lab.local_send_error(1058.0, 1418.0, errno.ENETUNREACH)
    lab.blackhole(1058.0, 1418.0, "s2c")
    return lab.run_client(until=T0 + 500.0)


def c2_long_loss_new_cgnat_port(monkeypatch):
    """MP0 (field-derived): as C1, but the CGNAT mapping changes during the
    outage -- same public IPv4, new public UDP port (54654 -> 54104)."""
    lab = Lab("C2_MP0_long_loss_new_cgnat_port", monkeypatch)
    lab.external_addr = CGNAT_BEFORE
    lab.local_send_error(1058.0, 1418.0, errno.ENETUNREACH)
    lab.blackhole(1058.0, 1418.0, "s2c")
    lab.remap(1238.0, CGNAT_AFTER)
    return lab.run_client(until=T0 + 500.0)


def c3_server_unreachable_beyond_bounds(monkeypatch):
    """MP0: silent bidirectional blackhole 1058-1300 (242 s, longer than
    peer_timeout) -- no local error, nothing delivered either way."""
    lab = Lab("C3_MP0_blackhole_242s", monkeypatch)
    lab.blackhole(1058.0, 1300.0)
    return lab.run_client(until=T0 + 400.0)


def c3b_forward_path_loss(monkeypatch):
    """Fable T06b: client->server lost 1055-1155 (server->client healthy)."""
    lab = Lab("C3b_T06b_forward_path_loss_100s", monkeypatch)
    lab.blackhole(1055.0, 1155.0, "c2s")
    return lab.run_client(until=T0 + 260.0)


def c5_server_restart(monkeypatch):
    """Fable T16: the server restarts at 1045 (fresh SecureState and a new
    listener incarnation, same identity key); client DATA now carries an
    unknown locator and is silently dropped."""
    lab = Lab("C5_T16_server_restart", monkeypatch)

    def restart(lab_):
        lab_.log("fault", "server restart: fresh SecureState, new listener")
        lab_.state = lab_.secure.SecureState(clock=lab_.clock)
        lab_.endpoint_token = lab_.secure._new_endpoint_token()
        lab_.owned_sessions = weakref.WeakValueDictionary()
        lab_.owned_pending = weakref.WeakValueDictionary()

    lab.at(1045.0, restart)
    return lab.run_client(until=T0 + 200.0)


SCENARIOS = {
    spec.mp0_id: spec
    for spec in (
        Scenario("A1", "T01", "A", "healthy baseline", a1_healthy),
        Scenario("A2", "T02", "A", "lost PING", a2_lost_ping),
        Scenario("A3", "T03", "A", "lost PONG", a3_lost_pong),
        Scenario("A4", "T04", "A", "5 s two-way blackhole", a4_short_blackhole),
        Scenario("A4b", "T04b", "A/C", "37 s blackhole past the deadline", a4b_blackhole_past_deadline),
        Scenario("A5a", "T05a", "A", "PONG 10 ms before deadline", a5a_pong_before_deadline),
        Scenario("A5b", "T05b", "A", "PONG at deadline", a5b_pong_at_deadline),
        Scenario("A5c", "T05c", "A", "PONG 10 ms after deadline", a5c_pong_after_deadline),
        Scenario("A5d", None, "A", "PONG at peer_timeout boundary", a5d_pong_at_peer_timeout_boundary),
        Scenario("A5e", None, "A/C", "PONG 10 ms after peer_timeout boundary", a5e_pong_after_peer_timeout_boundary),
        Scenario("A6", "T07", "A", "35 s client stall, PONG buffered", a6_client_stall),
        Scenario("A7", "T06a", "A", "reverse-path-only loss 100 s", a7_reverse_path_loss),
        Scenario("A9", "T14", "A", "refresh evidence, lost PONG", a9_refresh_evidence_with_lost_pong),
        Scenario("A9b", None, "A", "refresh-only evidence, every PONG lost", a9b_refresh_only_evidence),
        Scenario("A10", "T09d", "A/B", "ping overtakes PATH_RESPONSE", a10_ping_overtakes_path_response),
        Scenario("A10b", None, "A/B", "PATH_ACK-only evidence, every PONG lost", a10b_path_ack_only_evidence),
        Scenario("A11", None, "A/C", "2 s local ENETUNREACH", a11_short_local_send_error),
        Scenario("A11b", None, "A", "transient ICMP-derived receive error", a11b_transient_receive_error),
        Scenario("B1", "T08", "B", "healthy A->B migration", b1_migration),
        Scenario("B2", "T09a", "B", "lost PATH_CHALLENGE", b2_lost_challenge),
        Scenario("B2b", "T09a7", "B", "lost PATH_CHALLENGE, 7 s NMEA", b2b_lost_challenge_7s),
        Scenario("B3", "T09b", "B", "lost PATH_RESPONSE", b3_lost_response),
        Scenario("B4", "T09c", "B", "lost PATH_ACK", b4_lost_ack),
        Scenario("B6a", "T11b", "B", "refresh starts while candidate open", b6a_refresh_starts_while_candidate_open),
        Scenario("B6b", "T11c", "B", "stale E0 candidate after refresh", b6b_stale_candidate_after_refresh),
        Scenario("B7", "T11a", "B", "refresh then migration", b7_refresh_then_migration),
        Scenario("B8", "T10b", "B", "A->B->C with late old-path packet", b8_rapid_a_b_c),
        Scenario("B10", "T10a", "B", "flap A->B->A during challenge", b10_flap_during_challenge),
        Scenario("B11", "T12", "B", "canonical sockaddr (flowinfo vs port)", b11_canonical_sockaddr),
        Scenario("B12", None, "B", "every challenge attempt of one incarnation lost", b12_every_attempt_of_one_incarnation_lost),
        Scenario("C1", None, "C", "6 min local Wi-Fi loss, same tuple", c1_long_local_network_loss),
        Scenario("C2", None, "C", "6 min loss, new CGNAT port", c2_long_loss_new_cgnat_port),
        Scenario("C3", None, "C", "242 s silent blackhole", c3_server_unreachable_beyond_bounds),
        Scenario("C3b", "T06b", "C", "forward-path-only loss 100 s", c3b_forward_path_loss),
        Scenario("C5", "T16", "C", "server restart (unknown locator)", c5_server_restart),
    )
}


def run(mp0_id, monkeypatch):
    return SCENARIOS[mp0_id].build(monkeypatch)
