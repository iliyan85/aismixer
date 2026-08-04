import core.utils.ttlmap as ttlmap_module
from core.state.s_cache import SourceState


def test_source_state_tracks_only_truthy_sources_and_preserves_state():
    source_state = SourceState()

    source_state.touch_s(None)
    source_state.touch_s("")

    assert len(source_state._s_cache) == 0
    assert source_state._per_s_state == {}

    source_state.touch_s("station-a")
    associated_state = source_state._per_s_state["station-a"]
    associated_state["marker"] = True
    source_state.touch_s("station-a")

    assert len(source_state._s_cache) == 1
    assert source_state._per_s_state["station-a"] is associated_state
    assert associated_state == {"marker": True}


def test_source_state_ttl_eviction_removes_associated_state(monkeypatch):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    source_state = SourceState(ttl_seconds=1.0)
    source_state.touch_s("station-a")
    source_state._per_s_state["station-a"]["marker"] = True

    now_ns = 1_000_000_000

    assert not source_state._s_cache.contains("station-a")
    assert "station-a" not in source_state._per_s_state


def test_source_state_capacity_eviction_removes_associated_state():
    source_state = SourceState(max_entries=1)
    source_state.touch_s("station-a")
    source_state._per_s_state["station-a"]["marker"] = True

    source_state.touch_s("station-b")

    assert not source_state._s_cache.contains("station-a")
    assert "station-a" not in source_state._per_s_state
    assert source_state._s_cache.contains("station-b")
    assert source_state._per_s_state["station-b"] == {}


def test_source_state_eviction_callback_is_instance_owned():
    first_source_state = SourceState(max_entries=1)
    second_source_state = SourceState(max_entries=1)
    first_source_state.touch_s("shared-station")
    second_source_state.touch_s("shared-station")
    second_associated_state = second_source_state._per_s_state[
        "shared-station"
    ]
    second_associated_state["owner"] = "second"

    first_source_state.touch_s("first-only-station")

    assert "shared-station" not in first_source_state._per_s_state
    assert second_source_state._s_cache.contains("shared-station")
    assert second_source_state._per_s_state[
        "shared-station"
    ] is second_associated_state
    assert second_associated_state == {"owner": "second"}
