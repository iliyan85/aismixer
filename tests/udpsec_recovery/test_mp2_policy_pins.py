"""MP2 policy pins: the CURRENT path-migration recovery behaviour, exactly.

MP2 inverted the two pre-MP2 pins MP0 wrote:
- `test_pre_mp2_one_path_challenge_per_candidate_incarnation`;
- `test_pre_mp2_lost_challenge_or_response_waits_for_candidate_expiry`.

One live candidate incarnation now resends its SAME PATH_CHALLENGE (same
token, path_generation and address, freshly encrypted) at most
`PATH_CHALLENGE_MAX_SENDS` times in total, each attempt at least
`PATH_CHALLENGE_RETRY_INTERVAL_SECONDS` after the previous one, never at or
after its fixed deadline.

These tests pin that policy on the lab's real client and real server, with
the real retry pass run at its exact due instants (the lab's stand-in for
the listener's retry task). They are NOT invariants: they make any change of
this policy deliberate. Unit-level MP2 scenarios MP2-B1..MP2-B11 live in
tests/test_udpsec_path_challenge_retry.py.
"""

import pytest

from .harness import expect
from .harness.lab import ADDR_A, ADDR_B
from .harness.scenarios import run

INTERVAL = 2.0
MAX_SENDS = 4


def _challenges(lab):
    return [
        (p.t, p.message["path_generation"], p.message["challenge_token"], p.verdict)
        for p in lab.sent("s2c", "path_challenge")
    ]


def _same_path(lab, first, second):
    return lab.secure.normalize_sockaddr(first) == lab.secure.normalize_sockaddr(second)


def test_mp2_retry_constants(monkeypatch):
    secure = run("B1", monkeypatch).secure
    assert secure.PATH_CHALLENGE_RETRY_INTERVAL_SECONDS == INTERVAL
    assert secure.PATH_CHALLENGE_MAX_SENDS == MAX_SENDS
    assert secure.PATH_CANDIDATE_TTL_SECONDS == 10.0


@pytest.mark.parametrize(
    "scenario_id, sent, retries",
    [
        ("B1", 1, 0),
        ("B2", 2, 1),
        ("B2b", 2, 1),
        ("B3", 2, 1),
        ("B4", 1, 0),
        ("B10", 4, 3),
        ("B12", 5, 3),
    ],
)
def test_mp2_challenges_per_incarnation_are_bounded_and_identical(
    monkeypatch, scenario_id, sent, retries
):
    """Inverts `test_pre_mp2_one_path_challenge_per_candidate_incarnation`.
    Challenges sent = candidates opened (the initial sends) + retries. No
    incarnation exceeds MAX_SENDS attempts, every attempt of one incarnation
    carries its one token, and attempts are at least one interval apart."""
    lab = run(scenario_id, monkeypatch)
    stats = lab.state.stats()
    assert stats.migration_challenges_sent == sent
    assert stats.migration_challenge_retries_sent == retries
    assert stats.migration_challenges_sent == (
        stats.path_candidates_opened + stats.migration_challenge_retries_sent
    )
    by_generation = {}
    for t, generation, token, _verdict in _challenges(lab):
        by_generation.setdefault(generation, []).append((t, token))
    for attempts in by_generation.values():
        assert len(attempts) <= MAX_SENDS
        assert len({token for _t, token in attempts}) == 1
        times = [t for t, _token in attempts]
        assert all(later - earlier >= INTERVAL - 1e-9 for earlier, later in zip(times, times[1:]))


@pytest.mark.parametrize(
    "scenario_id, first_at, first_verdict",
    [
        ("B2", 1050.05, "DROP:hook"),
        ("B2b", 1049.05, "DROP:hook"),
        ("B3", 1050.05, "sent"),  # its PATH_RESPONSE is the lost packet
    ],
)
def test_mp2_lost_challenge_or_response_recovers_in_the_same_incarnation(
    monkeypatch, scenario_id, first_at, first_verdict
):
    """Inverts `test_pre_mp2_lost_challenge_or_response_waits_for_candidate_expiry`
    (pre-MP2: a new incarnation after candidate expiry, commit +10.1 s).
    The SAME challenge is retried one interval later and commits the SAME
    incarnation (generation 1) about 8 s earlier, on the same
    LogicalSession, epoch 0, with no rekey."""
    lab = run(scenario_id, monkeypatch)
    challenges = _challenges(lab)
    assert [(t, generation) for t, generation, _tok, _v in challenges] == pytest.approx(
        [(first_at, 1), (first_at + INTERVAL, 1)]
    )
    assert challenges[0][2] == challenges[1][2]
    assert [v for _t, _g, _tok, v in challenges] == [first_verdict, "sent"]
    ack = lab.first_sent("s2c", "path_ack")
    assert ack.t == pytest.approx(first_at + INTERVAL + 0.1)
    assert ack.message["path_generation"] == 1
    stats = lab.state.stats()
    assert (
        stats.path_candidates_opened,
        stats.path_candidates_expired,
        stats.path_migrations_committed,
    ) == (1, 0, 1)
    session = lab.original_server_session()
    assert session.path_state.path_generation == 1
    assert session.path_state.active_path_generation == 1
    assert session.current_epoch.generation == 0
    expect.assert_logical_session_survived(lab)


@pytest.mark.parametrize("scenario_id", ["B1", "B4"])
def test_mp2_prompt_commit_sends_no_retry(monkeypatch, scenario_id):
    """B1 commits at 1050.15, B4 at 1050.1 with its PATH_ACK lost (the
    client learns from its next PONG, unchanged); both commits come before
    the first retry would be due (1052.05), so none is ever sent."""
    lab = run(scenario_id, monkeypatch)
    assert [(t, g) for t, g, _tok, _v in _challenges(lab)] == pytest.approx([(1050.05, 1)])
    assert lab.state.stats().migration_challenge_retries_sent == 0
    expect.assert_logical_session_survived(lab)


def test_mp2_exhausted_incarnation_expires_on_time_then_a_new_one_commits(monkeypatch):
    """B12: every attempt of generation 1 is lost -- 1050.05, 1052.05,
    1054.05, 1056.05, then the budget is spent. It expires at its unchanged
    deadline (1060.05); the authenticated NMEA from B arriving then opens
    generation 2, which commits at 1060.15. Nothing ever re-arms generation
    1, and the session is never rekeyed."""
    lab = run("B12", monkeypatch)
    challenges = _challenges(lab)
    assert [(t, g, v) for t, g, _tok, v in challenges] == [
        (pytest.approx(1050.05), 1, "DROP:hook"),
        (pytest.approx(1052.05), 1, "DROP:hook"),
        (pytest.approx(1054.05), 1, "DROP:hook"),
        (pytest.approx(1056.05), 1, "DROP:hook"),
        (pytest.approx(1060.05), 2, "sent"),
    ]
    assert len({tok for _t, g, tok, _v in challenges if g == 1}) == 1
    ack = lab.first_sent("s2c", "path_ack")
    assert (ack.t, ack.message["path_generation"]) == (pytest.approx(1060.15), 2)
    stats = lab.state.stats()
    assert (stats.path_candidates_opened, stats.path_candidates_expired) == (2, 1)
    expect.assert_logical_session_survived(lab)


def test_mp2_retries_to_a_path_the_client_left_change_nothing(monkeypatch):
    """B10: after the flap back to A at 1052, the three retries to B
    (1052.05, 1054.05, 1056.05) are undeliverable. The response the client
    sent from A still cannot commit; the candidate expires at its unchanged
    deadline and A stays active."""
    lab = run("B10", monkeypatch)
    challenges = _challenges(lab)
    assert [v for _t, _g, _tok, v in challenges] == ["sent"] + ["DROP:unreachable-old-path"] * 3
    assert {g for _t, g, _tok, _v in challenges} == {1}
    stats = lab.state.stats()
    assert (stats.path_migrations_committed, stats.path_candidates_expired) == (0, 1)
    assert _same_path(lab, lab.original_server_session().path_state.active_path, ADDR_A)
    assert not _same_path(lab, ADDR_A, ADDR_B)
