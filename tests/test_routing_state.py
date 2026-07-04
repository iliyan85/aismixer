import pytest

from core.routing import RoutingTable
from core.routing_state import (
    RoutingSnapshot,
    RoutingState,
    StaleRoutingGenerationError,
)


def make_table(target_id="udp:aishub"):
    return RoutingTable.from_definitions(
        {"trusted": {"include": ["udp:source_a"]}},
        [
            {
                "name": f"trusted_to_{target_id}",
                "from_zone": "trusted",
                "to": [target_id],
            }
        ],
    )


def test_default_state_starts_at_generation_zero_with_no_table():
    state = RoutingState()

    snapshot = state.snapshot()

    assert snapshot == RoutingSnapshot(generation=0, table=None)


def test_initial_routing_table_is_exposed_at_generation_zero():
    table = make_table()
    state = RoutingState(initial_table=table)

    snapshot = state.snapshot()

    assert snapshot.generation == 0
    assert snapshot.table is table


def test_snapshot_retrieval_does_not_increment_generation():
    state = RoutingState(make_table())

    first = state.snapshot()
    second = state.snapshot()

    assert first is second
    assert first.generation == 0
    assert second.generation == 0


def test_snapshot_is_immutable():
    snapshot = RoutingState().snapshot()

    with pytest.raises(AttributeError):
        snapshot.generation = 99


def test_replace_installs_new_table_and_increments_generation():
    state = RoutingState()
    table = make_table()

    snapshot = state.replace(table)

    assert snapshot.generation == 1
    assert snapshot.table is table
    assert state.snapshot() is snapshot


def test_replace_none_disables_routing_and_increments_generation():
    state = RoutingState(make_table())

    snapshot = state.replace(None)

    assert snapshot.generation == 1
    assert snapshot.table is None


def test_old_snapshot_remains_unchanged_after_replacement():
    first_table = make_table("udp:first")
    second_table = make_table("udp:second")
    state = RoutingState(first_table)
    old_snapshot = state.snapshot()

    new_snapshot = state.replace(second_table)

    assert old_snapshot.generation == 0
    assert old_snapshot.table is first_table
    assert new_snapshot.generation == 1
    assert new_snapshot.table is second_table


def test_replacement_with_matching_expected_generation_succeeds():
    state = RoutingState()
    expected_generation = state.snapshot().generation
    table = make_table()

    snapshot = state.replace(table, expected_generation=expected_generation)

    assert snapshot.generation == expected_generation + 1
    assert snapshot.table is table


def test_replacement_with_stale_expected_generation_raises_dedicated_error():
    state = RoutingState()
    state.replace(make_table("udp:first"))

    with pytest.raises(StaleRoutingGenerationError) as exc_info:
        state.replace(make_table("udp:second"), expected_generation=0)

    assert exc_info.value.expected_generation == 0
    assert exc_info.value.actual_generation == 1
    assert "expected 0, actual 1" in str(exc_info.value)


def test_stale_replacement_leaves_state_unchanged():
    first_table = make_table("udp:first")
    state = RoutingState()
    installed = state.replace(first_table)

    with pytest.raises(StaleRoutingGenerationError):
        state.replace(make_table("udp:second"), expected_generation=0)

    assert state.snapshot() is installed
    assert state.snapshot().generation == 1
    assert state.snapshot().table is first_table


def test_invalid_table_type_is_rejected():
    state = RoutingState()

    with pytest.raises(TypeError, match="RoutingTable or None"):
        state.replace({"not": "a routing table"})


def test_invalid_replacement_leaves_state_unchanged():
    table = make_table()
    state = RoutingState(table)
    snapshot = state.snapshot()

    with pytest.raises(TypeError):
        state.replace(object())

    assert state.snapshot() is snapshot
    assert state.snapshot().generation == 0
    assert state.snapshot().table is table


def test_consecutive_successful_replacements_increment_generations_monotonically():
    state = RoutingState()

    first = state.replace(make_table("udp:first"))
    second = state.replace(make_table("udp:second"))
    third = state.replace(None)

    assert (first.generation, second.generation, third.generation) == (1, 2, 3)


def test_replacing_with_same_table_object_still_creates_new_generation():
    table = make_table()
    state = RoutingState(table)

    snapshot = state.replace(table)

    assert snapshot.generation == 1
    assert snapshot.table is table


def test_independent_routing_state_instances_do_not_share_state():
    first_state = RoutingState()
    second_state = RoutingState()
    first_table = make_table("udp:first")
    second_table = make_table("udp:second")

    first_snapshot = first_state.replace(first_table)
    second_snapshot = second_state.replace(second_table)

    assert first_snapshot.generation == 1
    assert second_snapshot.generation == 1
    assert first_state.snapshot().table is first_table
    assert second_state.snapshot().table is second_table
