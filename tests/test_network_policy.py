from dataclasses import FrozenInstanceError

import pytest

from core.network_policy import (
    NetworkPolicy,
    NetworkPolicyConfigError,
    compile_ingress_policy,
)


def test_absent_policy_is_unrestricted():
    policy = compile_ingress_policy({}, context="udp_inputs[0]")

    assert policy.is_unrestricted
    assert not policy.is_deny_all
    assert policy.allows("192.0.2.15")
    assert policy.allows("2001:db8::15")


def test_empty_list_denies_all():
    policy = compile_ingress_policy({"allow_from": []}, context="udp_inputs[0]")

    assert not policy.is_unrestricted
    assert policy.is_deny_all
    assert not policy.allows("192.0.2.15")
    assert not policy.allows("2001:db8::15")


def test_exact_ipv4_and_ipv6_matches_are_allowed():
    policy = NetworkPolicy.from_entries(
        ["192.0.2.15", "2001:db8::15"],
        context="test.allow_from",
    )

    assert policy.allows("192.0.2.15")
    assert policy.allows("2001:db8::15")
    assert not policy.allows("192.0.2.16")
    assert not policy.allows("2001:db8::16")


def test_ipv4_and_ipv6_cidr_matches_are_allowed():
    policy = NetworkPolicy.from_entries(
        ["198.51.100.0/24", "2001:db8:42::/64"],
        context="test.allow_from",
    )

    assert policy.allows("198.51.100.44")
    assert policy.allows("2001:db8:42::1234")
    assert not policy.allows("198.51.101.44")
    assert not policy.allows("2001:db8:43::1234")


def test_ipv4_mapped_ipv6_peer_can_match_ipv4_rule():
    policy = NetworkPolicy.from_entries(
        ["192.0.2.0/24"],
        context="test.allow_from",
    )

    assert policy.allows("::ffff:192.0.2.15")
    assert not policy.allows("::ffff:198.51.100.15")


@pytest.mark.parametrize(
    "entry",
    [
        "receiver.example.net",
        "192.0.2.1/33",
        "2001:db8::1/129",
        "192.0.2.15/24",
        "",
        7,
    ],
)
def test_malformed_entries_are_rejected(entry):
    with pytest.raises(NetworkPolicyConfigError, match="allow_from"):
        NetworkPolicy.from_entries([entry], context="udp_inputs[0].allow_from")


def test_allow_from_must_be_a_list():
    with pytest.raises(NetworkPolicyConfigError, match="udp_inputs\\[0\\].allow_from"):
        compile_ingress_policy(
            {"allow_from": "192.0.2.15"},
            context="udp_inputs[0]",
        )


def test_null_allow_from_is_rejected_with_context():
    with pytest.raises(NetworkPolicyConfigError, match="sec_inputs\\[1\\].allow_from"):
        compile_ingress_policy(
            {"allow_from": None},
            context="sec_inputs[1]",
        )


def test_policy_is_immutable():
    policy = NetworkPolicy.from_entries(
        ["192.0.2.15"],
        context="test.allow_from",
    )

    with pytest.raises(FrozenInstanceError):
        policy._networks = ()
    with pytest.raises(TypeError):
        policy.networks[0] = policy.networks[0]
