import core.utils.ttlmap as ttlmap_module
from core.utils.ttlmap import TTLMap


def test_clear_discards_live_entries_preserves_config_and_reuses_map(
    monkeypatch,
):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    callback = evicted.append
    ttl_map = TTLMap(
        ttl_seconds=1.0,
        max_entries=2,
        on_evict=callback,
        sweep_every_seconds=7.0,
        ops_per_sweep=99,
    )
    configuration = (
        ttl_map._ttl_ns,
        ttl_map._max_entries,
        ttl_map._on_evict,
        ttl_map._sweep_every_ns,
        ttl_map._ops_per_sweep,
    )

    ttl_map.touch("station-a")
    now_ns = 100
    ttl_map.touch("station-a")
    ttl_map.touch("station-b")

    assert len(ttl_map) == 2
    assert len(ttl_map._q) == 3

    assert ttl_map.clear() == 2
    assert ttl_map._d == {}
    assert not ttl_map._q
    assert ttl_map._ops == 0
    assert ttl_map._last_sweep_ns == now_ns
    assert (
        ttl_map._ttl_ns,
        ttl_map._max_entries,
        ttl_map._on_evict,
        ttl_map._sweep_every_ns,
        ttl_map._ops_per_sweep,
    ) == configuration
    assert evicted == ["station-a", "station-b"]

    assert ttl_map.clear() == 0
    assert evicted == ["station-a", "station-b"]

    ttl_map.touch("fresh-station")
    now_ns += 999_999_999
    assert ttl_map.contains("fresh-station")
    now_ns += 1
    assert not ttl_map.contains("fresh-station")
    assert evicted == ["station-a", "station-b", "fresh-station"]


def test_clear_does_not_callback_stale_expiry_records(monkeypatch):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1.0,
        on_evict=evicted.append,
        sweep_every_seconds=100.0,
        ops_per_sweep=100,
    )

    ttl_map.touch("expired")
    now_ns = 1_000_000_000
    assert not ttl_map.contains("expired")
    assert evicted == ["expired"]
    assert len(ttl_map._q) == 1

    ttl_map.touch("live")
    now_ns += 1
    ttl_map.touch("live")

    assert len(ttl_map) == 1
    assert len(ttl_map._q) == 3
    assert ttl_map.clear() == 1
    assert evicted == ["expired", "live"]
    assert not ttl_map._q

    ttl_map.touch("expired")
    assert ttl_map.contains("expired")
    assert evicted == ["expired", "live"]
