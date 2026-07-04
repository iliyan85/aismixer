import pytest

from core.target_identity import build_udp_target_id


def test_build_udp_target_id_returns_canonical_namespaced_id():
    assert build_udp_target_id("aishub") == "udp:aishub"
    assert build_udp_target_id("local_debug") == "udp:local_debug"


def test_build_udp_target_id_does_not_apply_tag_s_sanitizing_or_truncation():
    configured_id = "long target name / not nmea-tag-safe and more than 15 chars"

    assert build_udp_target_id(configured_id) == f"udp:{configured_id}"


@pytest.mark.parametrize("configured_id", ["", "   "])
def test_build_udp_target_id_rejects_empty_ids(configured_id):
    with pytest.raises(ValueError, match="non-empty"):
        build_udp_target_id(configured_id)


@pytest.mark.parametrize("configured_id", [None, 123])
def test_build_udp_target_id_rejects_non_string_ids(configured_id):
    with pytest.raises(TypeError, match="string"):
        build_udp_target_id(configured_id)


def test_build_udp_target_id_rejects_already_namespaced_id():
    with pytest.raises(ValueError, match="unnamespaced"):
        build_udp_target_id("udp:aishub")
