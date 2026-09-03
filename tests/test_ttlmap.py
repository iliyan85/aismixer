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

    assert ttl_map.clear() == 2
    assert ttl_map._d == {}
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

    ttl_map.touch("live")
    now_ns += 1
    ttl_map.touch("live")

    assert len(ttl_map) == 1
    assert ttl_map.clear() == 1
    assert evicted == ["expired", "live"]

    ttl_map.touch("expired")
    assert ttl_map.contains("expired")
    assert evicted == ["expired", "live"]


def test_refresh_extends_expiry_and_stale_record_cannot_evict(monkeypatch):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1.0,
        on_evict=evicted.append,
        sweep_every_seconds=1000.0,
        ops_per_sweep=1_000_000_000,
    )

    ttl_map.touch("station")
    now_ns = 500_000_000
    ttl_map.touch("station")

    now_ns = 1_000_000_000
    assert ttl_map.contains("station")
    assert evicted == []

    now_ns = 1_500_000_001
    assert not ttl_map.contains("station")
    assert evicted == ["station"]


def test_repeated_touch_of_one_key_bounds_bookkeeping_and_calls_back_once(
    monkeypatch,
):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1000.0,
        max_entries=5,
        on_evict=evicted.append,
        sweep_every_seconds=1000.0,
        ops_per_sweep=1_000_000_000,
    )

    touch_count = 100_000
    for i in range(touch_count):
        now_ns = i  # nanoseconds apart, all well inside the 1000s TTL window
        ttl_map.touch("hot-key")

    # Retained bookkeeping is exactly one logical record, independent of how
    # many times the key was touched.
    assert len(ttl_map) == 1
    assert len(ttl_map._d) == 1

    now_ns = (touch_count - 1) + ttl_map._ttl_ns + 1
    assert not ttl_map.contains("hot-key")
    assert evicted == ["hot-key"]


def test_capacity_eviction_evicts_oldest_key_at_capacity(monkeypatch):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1000.0,
        max_entries=1,
        on_evict=evicted.append,
        sweep_every_seconds=1000.0,
        ops_per_sweep=1_000_000_000,
    )

    ttl_map.touch("A")
    now_ns = 1
    ttl_map.touch("B")

    assert not ttl_map.contains("A")
    assert ttl_map.contains("B")
    assert evicted == ["A"]


def test_touching_existing_key_at_capacity_does_not_evict(monkeypatch):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1000.0,
        max_entries=1,
        on_evict=evicted.append,
        sweep_every_seconds=1000.0,
        ops_per_sweep=1_000_000_000,
    )

    ttl_map.touch("A")
    now_ns = 1
    ttl_map.touch("A")

    assert ttl_map.contains("A")
    assert evicted == []


def test_expired_entry_reclaimed_before_new_admission_at_capacity(monkeypatch):
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1.0,
        max_entries=1,
        on_evict=evicted.append,
        sweep_every_seconds=1000.0,
        ops_per_sweep=1_000_000_000,
    )

    ttl_map.touch("A")
    now_ns = 2_000_000_000
    ttl_map.touch("B")

    assert not ttl_map.contains("A")
    assert ttl_map.contains("B")
    assert evicted == ["A"]


def test_capacity_eviction_does_not_evict_refreshed_key(monkeypatch):
    """Regression for Point 3: a stale queue record for an already-refreshed
    key must not cause that key's current generation to be evicted."""
    now_ns = 0
    monkeypatch.setattr(ttlmap_module, "_MONO", lambda: now_ns)
    evicted = []
    ttl_map = TTLMap(
        ttl_seconds=1000.0,
        max_entries=2,
        on_evict=evicted.append,
        sweep_every_seconds=1000.0,
        ops_per_sweep=1_000_000_000,
    )

    now_ns = 0
    ttl_map.touch("A")
    now_ns = 1
    ttl_map.touch("B")
    now_ns = 2
    ttl_map.touch("A")
    now_ns = 3
    ttl_map.touch("C")

    assert ttl_map.contains("A")
    assert not ttl_map.contains("B")
    assert ttl_map.contains("C")
    assert evicted == ["B"]
