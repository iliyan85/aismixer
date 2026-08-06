import json

import pytest

from core.metrics import (
    EgressMetricsSnapshot,
    ProcessorMetricsSnapshot,
    QueueMetricsSnapshot,
    RuntimeStatisticsSnapshot,
)
from core.routing_control import (
    RoutingCandidateConfigError,
    RoutingControlService,
    RoutingControlStatus,
)
from core.routing_control_protocol import (
    ERROR_INVALID_REQUEST,
    ERROR_INVALID_ROUTING_CONFIG,
    ERROR_MALFORMED_JSON,
    ERROR_STALE_GENERATION,
    ERROR_UNKNOWN_METHOD,
    ERROR_UNSUPPORTED_VERSION,
    METHOD_RUNTIME_STATISTICS,
    ROUTING_CONTROL_PROTOCOL_VERSION,
    RoutingControlProtocol,
    build_error_response,
    decode_json_request,
    encode_json_response,
)
from core.routing_state import RoutingState
from core.runtime_routing import compile_routing_section


TARGET_ID_BY_NAME = {
    "udp:a": 0,
    "udp:b": 1,
    "udp:c": 2,
}


def queue_metrics(
    name,
    *,
    capacity=1,
    depth=0,
    peak_depth=0,
    enqueued=0,
    dequeued=0,
    put_waits=0,
    current_put_waiters=0,
):
    return QueueMetricsSnapshot(
        name=name,
        capacity=capacity,
        depth=depth,
        peak_depth=peak_depth,
        enqueued=enqueued,
        dequeued=dequeued,
        put_waits=put_waits,
        current_put_waiters=current_put_waiters,
    )


def zero_runtime_statistics_snapshot():
    return RuntimeStatisticsSnapshot(
        ingress_queues=(),
        processing_queue=queue_metrics("processing", capacity=1024),
        processor=ProcessorMetricsSnapshot(
            process_calls=0,
            process_completed=0,
            process_failed=0,
            process_in_flight=0,
            outputless_calls=0,
            output_batches=0,
            output_messages=0,
            reset_calls=0,
            reset_completed=0,
            reset_failed=0,
            reset_in_flight=0,
        ),
        egress_queue=queue_metrics("egress", capacity=1),
        egress_operations=EgressMetricsSnapshot(
            batches_started=0,
            batches_completed=0,
            batches_failed=0,
            batches_cancelled=0,
            active_batches=0,
            outputs_started=0,
            outputs_completed=0,
            outputs_failed=0,
            outputs_cancelled=0,
            active_outputs=0,
        ),
    )


class RecordingStatisticsSource:
    def __init__(self, snapshot=None):
        self._snapshot = (
            zero_runtime_statistics_snapshot() if snapshot is None else snapshot
        )
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return self._snapshot


def routing_section(routes=None, zones=None):
    return {
        "zones": zones
        or {
            "source": {"include": ["udp:source"]},
            "backup": {"include": ["udp:backup"]},
        },
        "routes": routes
        or [
            {
                "name": "source_to_a",
                "from_zone": "source",
                "to": ["udp:a"],
            }
        ],
    }


def make_service(initial_section=None):
    initial_table = None
    if initial_section is not None:
        initial_table = compile_routing_section(
            initial_section,
            TARGET_ID_BY_NAME,
        )
    state = RoutingState(initial_table)
    return state, RoutingControlService(state, TARGET_ID_BY_NAME)


def make_protocol(initial_section=None, statistics=None):
    state, service = make_service(initial_section)
    statistics = (
        RecordingStatisticsSource() if statistics is None else statistics
    )
    return state, RoutingControlProtocol(service, statistics)


def status_request(request_id="req-1"):
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": "routing.status",
    }


def runtime_statistics_request(request_id="req-1"):
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": METHOD_RUNTIME_STATISTICS,
    }


def replace_request(request_id="req-1", section=None, expected_generation=None):
    params = {"routing": section or routing_section()}
    if expected_generation is not None:
        params["expected_generation"] = expected_generation
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": "routing.replace",
        "params": params,
    }


def disable_request(request_id="req-1", expected_generation=None):
    request = {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": "routing.disable",
    }
    if expected_generation is not None:
        request["params"] = {"expected_generation": expected_generation}
    return request


def parse_response(data):
    return json.loads(data.decode("utf-8"))


def assert_error(response, code, request_id="req-1"):
    assert response["version"] == ROUTING_CONTROL_PROTOCOL_VERSION
    assert response["request_id"] == request_id
    assert response["ok"] is False
    assert response["error"]["code"] == code


def test_runtime_statistics_request_has_exact_no_params_shape():
    assert runtime_statistics_request("stats-1") == {
        "version": 1,
        "request_id": "stats-1",
        "method": "runtime.statistics",
    }


@pytest.mark.parametrize("params", [{}, None, [], {"extra": True}])
def test_runtime_statistics_rejects_any_params_without_pulling(params):
    statistics = RecordingStatisticsSource()
    _state, protocol = make_protocol(statistics=statistics)
    request = runtime_statistics_request()
    request["params"] = params

    response = protocol.handle_request(request)

    assert_error(response, ERROR_INVALID_REQUEST)
    assert response["error"]["message"] == (
        "Method 'runtime.statistics' does not accept params."
    )
    assert statistics.snapshot_calls == 0


def test_runtime_statistics_serializes_exact_all_zero_result_and_calls_once():
    class RoutingServiceMustNotBeCalled:
        def status(self):
            raise AssertionError("routing service must not be called")

        def replace_from_config(self, routing_config, expected_generation=None):
            raise AssertionError("routing service must not be called")

        def disable(self, expected_generation=None):
            raise AssertionError("routing service must not be called")

    statistics = RecordingStatisticsSource()
    protocol = RoutingControlProtocol(
        RoutingServiceMustNotBeCalled(),
        statistics,
    )

    response = parse_response(
        protocol.handle_json(json.dumps(runtime_statistics_request("stats-zero")))
    )

    assert statistics.snapshot_calls == 1
    assert response == {
        "version": 1,
        "request_id": "stats-zero",
        "ok": True,
        "result": {
            "ingress_queues": [],
            "processing_queue": {
                "name": "processing",
                "capacity": 1024,
                "depth": 0,
                "peak_depth": 0,
                "enqueued": 0,
                "dequeued": 0,
                "put_waits": 0,
                "current_put_waiters": 0,
            },
            "processor": {
                "process_calls": 0,
                "process_completed": 0,
                "process_failed": 0,
                "process_in_flight": 0,
                "outputless_calls": 0,
                "output_batches": 0,
                "output_messages": 0,
                "reset_calls": 0,
                "reset_completed": 0,
                "reset_failed": 0,
                "reset_in_flight": 0,
            },
            "egress_queue": {
                "name": "egress",
                "capacity": 1,
                "depth": 0,
                "peak_depth": 0,
                "enqueued": 0,
                "dequeued": 0,
                "put_waits": 0,
                "current_put_waiters": 0,
            },
            "egress_operations": {
                "batches_started": 0,
                "batches_completed": 0,
                "batches_failed": 0,
                "batches_cancelled": 0,
                "active_batches": 0,
                "outputs_started": 0,
                "outputs_completed": 0,
                "outputs_failed": 0,
                "outputs_cancelled": 0,
                "active_outputs": 0,
            },
        },
    }


def test_runtime_statistics_serializes_populated_snapshot_in_ingress_order():
    snapshot = RuntimeStatisticsSnapshot(
        ingress_queues=(
            queue_metrics(
                "udp-ingress:0:station-a",
                capacity=8,
                depth=2,
                peak_depth=5,
                enqueued=10,
                dequeued=8,
                put_waits=3,
                current_put_waiters=1,
            ),
            queue_metrics(
                "udpsec-ingress:0:station-b",
                capacity=4,
                depth=0,
                peak_depth=4,
                enqueued=7,
                dequeued=7,
                put_waits=2,
            ),
        ),
        processing_queue=queue_metrics(
            "processing",
            capacity=16,
            depth=3,
            peak_depth=9,
            enqueued=20,
            dequeued=17,
            put_waits=4,
            current_put_waiters=2,
        ),
        processor=ProcessorMetricsSnapshot(
            process_calls=10,
            process_completed=8,
            process_failed=1,
            process_in_flight=1,
            outputless_calls=3,
            output_batches=5,
            output_messages=12,
            reset_calls=4,
            reset_completed=2,
            reset_failed=1,
            reset_in_flight=1,
        ),
        egress_queue=queue_metrics(
            "egress",
            capacity=1,
            depth=1,
            peak_depth=1,
            enqueued=6,
            dequeued=5,
            put_waits=2,
            current_put_waiters=1,
        ),
        egress_operations=EgressMetricsSnapshot(
            batches_started=9,
            batches_completed=5,
            batches_failed=1,
            batches_cancelled=1,
            active_batches=2,
            outputs_started=12,
            outputs_completed=7,
            outputs_failed=2,
            outputs_cancelled=1,
            active_outputs=2,
        ),
    )
    statistics = RecordingStatisticsSource(snapshot)
    _state, protocol = make_protocol(statistics=statistics)

    response = protocol.handle_request(runtime_statistics_request("stats-full"))

    assert statistics.snapshot_calls == 1
    assert response == {
        "version": 1,
        "request_id": "stats-full",
        "ok": True,
        "result": {
            "ingress_queues": [
                {
                    "name": "udp-ingress:0:station-a",
                    "capacity": 8,
                    "depth": 2,
                    "peak_depth": 5,
                    "enqueued": 10,
                    "dequeued": 8,
                    "put_waits": 3,
                    "current_put_waiters": 1,
                },
                {
                    "name": "udpsec-ingress:0:station-b",
                    "capacity": 4,
                    "depth": 0,
                    "peak_depth": 4,
                    "enqueued": 7,
                    "dequeued": 7,
                    "put_waits": 2,
                    "current_put_waiters": 0,
                },
            ],
            "processing_queue": {
                "name": "processing",
                "capacity": 16,
                "depth": 3,
                "peak_depth": 9,
                "enqueued": 20,
                "dequeued": 17,
                "put_waits": 4,
                "current_put_waiters": 2,
            },
            "processor": {
                "process_calls": 10,
                "process_completed": 8,
                "process_failed": 1,
                "process_in_flight": 1,
                "outputless_calls": 3,
                "output_batches": 5,
                "output_messages": 12,
                "reset_calls": 4,
                "reset_completed": 2,
                "reset_failed": 1,
                "reset_in_flight": 1,
            },
            "egress_queue": {
                "name": "egress",
                "capacity": 1,
                "depth": 1,
                "peak_depth": 1,
                "enqueued": 6,
                "dequeued": 5,
                "put_waits": 2,
                "current_put_waiters": 1,
            },
            "egress_operations": {
                "batches_started": 9,
                "batches_completed": 5,
                "batches_failed": 1,
                "batches_cancelled": 1,
                "active_batches": 2,
                "outputs_started": 12,
                "outputs_completed": 7,
                "outputs_failed": 2,
                "outputs_cancelled": 1,
                "active_outputs": 2,
            },
        },
    }


def test_routing_methods_do_not_pull_runtime_statistics():
    statistics = RecordingStatisticsSource()
    _state, protocol = make_protocol(statistics=statistics)

    status = protocol.handle_request(status_request("status"))
    replace = protocol.handle_request(replace_request("replace"))
    disable = protocol.handle_request(disable_request("disable"))

    assert status["ok"] is True
    assert replace["ok"] is True
    assert disable["ok"] is True
    assert statistics.snapshot_calls == 0


@pytest.mark.parametrize(
    ("raw_request", "error_code"),
    [
        (
            {
                "version": 2,
                "request_id": "stats-version",
                "method": "runtime.statistics",
            },
            ERROR_UNSUPPORTED_VERSION,
        ),
        (
            {
                "version": 1,
                "request_id": "stats-unknown",
                "method": "runtime.statistics.extra",
            },
            ERROR_UNKNOWN_METHOD,
        ),
    ],
)
def test_runtime_statistics_keeps_version_and_unknown_method_compatibility(
    raw_request,
    error_code,
):
    statistics = RecordingStatisticsSource()
    _state, protocol = make_protocol(statistics=statistics)

    response = protocol.handle_request(raw_request)

    assert ROUTING_CONTROL_PROTOCOL_VERSION == 1
    assert_error(response, error_code, request_id=raw_request["request_id"])
    assert statistics.snapshot_calls == 0


@pytest.mark.parametrize(
    ("raw_request", "code", "request_id"),
    [
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.status",
                "extra": True,
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        ({"request_id": "req-1", "method": "routing.status"}, ERROR_INVALID_REQUEST, "req-1"),
        ({"version": 1, "method": "routing.status"}, ERROR_INVALID_REQUEST, None),
        ({"version": 1, "request_id": "req-1"}, ERROR_INVALID_REQUEST, "req-1"),
        ({"version": 1, "request_id": "", "method": "routing.status"}, ERROR_INVALID_REQUEST, None),
        ({"version": 1, "request_id": 7, "method": "routing.status"}, ERROR_INVALID_REQUEST, None),
        ({"version": 1, "request_id": "req-1", "method": ""}, ERROR_INVALID_REQUEST, "req-1"),
        ({"version": 1, "request_id": "req-1", "method": 7}, ERROR_INVALID_REQUEST, "req-1"),
        ({"version": "1", "request_id": "req-1", "method": "routing.status"}, ERROR_INVALID_REQUEST, "req-1"),
        ({"version": True, "request_id": "req-1", "method": "routing.status"}, ERROR_INVALID_REQUEST, "req-1"),
        (
            {"version": 1, "request_id": "req-1", "method": "routing.replace"},
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.replace",
                "params": [],
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.replace",
                "params": {},
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.replace",
                "params": {"routing": routing_section(), "extra": True},
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.disable",
                "params": [],
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.disable",
                "params": {"extra": True},
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            replace_request(expected_generation=-1),
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            replace_request(expected_generation=True),
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
        (
            {"version": 2, "request_id": "req-1", "method": "routing.status"},
            ERROR_UNSUPPORTED_VERSION,
            "req-1",
        ),
        (
            {"version": 1, "request_id": "req-1", "method": "routing.reload"},
            ERROR_UNKNOWN_METHOD,
            "req-1",
        ),
        (
            {
                "version": 1,
                "request_id": "req-1",
                "method": "routing.status",
                "params": {},
            },
            ERROR_INVALID_REQUEST,
            "req-1",
        ),
    ],
)
def test_schema_rejections_are_deterministic(raw_request, code, request_id):
    _state, protocol = make_protocol()

    response = protocol.handle_request(raw_request)

    assert_error(response, code, request_id=request_id)


def test_disable_rejects_negative_expected_generation():
    _state, protocol = make_protocol()

    response = protocol.handle_request(disable_request(expected_generation=-1))

    assert_error(response, ERROR_INVALID_REQUEST)


def test_disable_rejects_bool_expected_generation():
    _state, protocol = make_protocol()

    response = protocol.handle_request(disable_request(expected_generation=True))

    assert_error(response, ERROR_INVALID_REQUEST)


def test_status_success_while_routing_is_disabled():
    _state, protocol = make_protocol()

    response = protocol.handle_request(status_request())

    assert response == {
        "version": 1,
        "request_id": "req-1",
        "ok": True,
        "result": {
            "generation": 0,
            "enabled": False,
            "zone_names": [],
            "route_names": [],
            "target_ids": [],
        },
    }


def test_status_success_while_routing_is_enabled():
    _state, protocol = make_protocol(routing_section())

    response = protocol.handle_request(status_request())

    assert response["ok"] is True
    assert response["result"]["generation"] == 0
    assert response["result"]["enabled"] is True
    assert response["result"]["zone_names"] == ["backup", "source"]
    assert response["result"]["route_names"] == ["source_to_a"]
    assert response["result"]["target_ids"] == ["udp:a"]


def test_valid_replace_request_installs_new_table():
    state, protocol = make_protocol()

    response = protocol.handle_request(replace_request())

    assert response["ok"] is True
    assert state.snapshot().table.match("udp:source").target_ids == ("udp:a",)
    assert state.snapshot().table.match_target_ids("udp:source") == (0,)


def test_replace_response_reports_exact_installed_generation():
    _state, protocol = make_protocol()

    response = protocol.handle_request(replace_request())

    assert response["result"]["generation"] == 1


def test_valid_disable_request_disables_routing():
    state, protocol = make_protocol(routing_section())

    response = protocol.handle_request(disable_request())

    assert response["ok"] is True
    assert response["result"]["enabled"] is False
    assert response["result"]["generation"] == 1
    assert state.snapshot().table is None


def test_matching_expected_generation_succeeds():
    _state, protocol = make_protocol()

    response = protocol.handle_request(replace_request(expected_generation=0))

    assert response["ok"] is True
    assert response["result"]["generation"] == 1


def test_stale_replace_returns_stale_generation():
    _state, protocol = make_protocol()
    protocol.handle_request(replace_request(expected_generation=0))

    response = protocol.handle_request(replace_request(expected_generation=0))

    assert_error(response, ERROR_STALE_GENERATION)


def test_stale_disable_returns_stale_generation():
    _state, protocol = make_protocol(routing_section())

    response = protocol.handle_request(disable_request(expected_generation=99))

    assert_error(response, ERROR_STALE_GENERATION)


def test_stale_errors_contain_expected_and_actual_generations():
    _state, protocol = make_protocol()
    protocol.handle_request(replace_request(expected_generation=0))

    response = protocol.handle_request(replace_request(expected_generation=0))

    assert response["error"]["expected_generation"] == 0
    assert response["error"]["actual_generation"] == 1


@pytest.mark.parametrize(
    "section",
    [
        {"zones": {}, "routes": [], "extra": True},
        {
            "zones": {"bad": {"include": ["udp:source"], "union": ["other"]}},
            "routes": [],
        },
        {
            "zones": {"source": {"include": ["udp:source"]}},
            "routes": [{"name": "bad_route", "from_zone": "missing", "to": ["udp:a"]}],
        },
        {
            "zones": {
                "a": {"union": ["b"]},
                "b": {"union": ["a"]},
            },
            "routes": [],
        },
        {
            "zones": {"source": {"include": ["udp:source"]}},
            "routes": [{"name": 1, "from_zone": "source", "to": ["udp:a"]}],
        },
        {
            "zones": {"source": {"include": "udp:source"}},
            "routes": [],
        },
        {
            "zones": {"source": {"include": ["udp:source"]}},
            "routes": [{"name": "missing", "from_zone": "source", "to": ["udp:missing"]}],
        },
    ],
)
def test_invalid_candidate_configs_map_to_invalid_routing_config(section):
    _state, protocol = make_protocol()

    response = protocol.handle_request(replace_request(section=section))

    assert_error(response, ERROR_INVALID_ROUTING_CONFIG)


def test_failed_requests_leave_routing_state_unchanged():
    state, protocol = make_protocol(routing_section())
    before = state.snapshot()

    response = protocol.handle_request(
        replace_request(
            section={
                "zones": {"source": {"include": ["udp:source"]}},
                "routes": [
                    {
                        "name": "source_to_missing",
                        "from_zone": "source",
                        "to": ["udp:missing"],
                    }
                ],
            }
        )
    )

    assert_error(response, ERROR_INVALID_ROUTING_CONFIG)
    assert state.snapshot() is before


def test_request_id_is_echoed_in_success_and_error_responses():
    _state, protocol = make_protocol()

    success = protocol.handle_request(status_request(request_id="client-123"))
    error = protocol.handle_request(
        {"version": 1, "request_id": "client-456", "method": "routing.reload"}
    )

    assert success["request_id"] == "client-123"
    assert error["request_id"] == "client-456"


def test_status_ordering_is_preserved():
    routes = [
        {"name": "first", "from_zone": "source", "to": ["udp:b", "udp:a"]},
        {"name": "second", "from_zone": "backup", "to": ["udp:b", "udp:c"]},
    ]
    _state, protocol = make_protocol(routing_section(routes=routes))

    response = protocol.handle_request(status_request())

    assert response["result"]["route_names"] == ["first", "second"]
    assert response["result"]["target_ids"] == ["udp:b", "udp:a", "udp:c"]


def test_two_sequential_updates_observe_monotonic_generations():
    _state, protocol = make_protocol()

    first = protocol.handle_request(replace_request())
    second = protocol.handle_request(disable_request())

    assert first["result"]["generation"] == 1
    assert second["result"]["generation"] == 2


@pytest.mark.parametrize(
    "exception",
    [
        TypeError("programming defect"),
        ValueError("programming defect"),
        RuntimeError("programming defect"),
    ],
)
def test_unexpected_replace_exception_is_not_mislabeled_as_invalid_config(exception):
    class BrokenService:
        def replace_from_config(self, routing_config, expected_generation=None):
            raise exception

    protocol = RoutingControlProtocol(BrokenService(), RecordingStatisticsSource())

    with pytest.raises(type(exception), match="programming defect"):
        protocol.handle_request(replace_request())


def test_candidate_config_error_maps_to_invalid_routing_config():
    class InvalidCandidateService:
        def replace_from_config(self, routing_config, expected_generation=None):
            raise RoutingCandidateConfigError("invalid candidate")

    protocol = RoutingControlProtocol(
        InvalidCandidateService(),
        RecordingStatisticsSource(),
    )

    response = protocol.handle_request(replace_request(request_id="req-candidate"))

    assert_error(
        response,
        ERROR_INVALID_ROUTING_CONFIG,
        request_id="req-candidate",
    )
    assert response["error"]["message"] == "invalid candidate"


def test_replace_response_uses_returned_status_without_extra_status_lookup():
    class ReplaceOnlyService:
        def status(self):
            raise AssertionError("status must not be called")

        def replace_from_config(self, routing_config, expected_generation=None):
            return RoutingControlStatus(
                generation=7,
                enabled=True,
                zone_names=("source",),
                route_names=("route",),
                target_ids=("udp:a",),
            )

    protocol = RoutingControlProtocol(
        ReplaceOnlyService(),
        RecordingStatisticsSource(),
    )

    response = protocol.handle_request(replace_request())

    assert response["result"]["generation"] == 7


def test_disable_response_uses_returned_status_without_extra_status_lookup():
    class DisableOnlyService:
        def status(self):
            raise AssertionError("status must not be called")

        def disable(self, expected_generation=None):
            return RoutingControlStatus(
                generation=8,
                enabled=False,
                zone_names=(),
                route_names=(),
                target_ids=(),
            )

    protocol = RoutingControlProtocol(
        DisableOnlyService(),
        RecordingStatisticsSource(),
    )

    response = protocol.handle_request(disable_request())

    assert response["result"]["generation"] == 8


def test_decode_json_request_accepts_bytes_input():
    request = decode_json_request(
        b'{"version":1,"request_id":"req-1","method":"routing.status"}'
    )

    assert request["method"] == "routing.status"


def test_decode_json_request_accepts_string_input():
    request = decode_json_request(
        '{"version":1,"request_id":"req-1","method":"routing.status"}'
    )

    assert request["request_id"] == "req-1"


@pytest.mark.parametrize(
    "data",
    [
        b"\xff",
        "{",
        "[]",
        "7",
    ],
)
def test_handle_json_malformed_inputs_return_malformed_json(data):
    _state, protocol = make_protocol()

    response = parse_response(protocol.handle_json(data))

    assert_error(response, ERROR_MALFORMED_JSON, request_id=None)
    assert response["error"]["message"] == "Malformed JSON request."


def test_schema_valid_json_object_error_is_invalid_request():
    _state, protocol = make_protocol()

    response = parse_response(
        protocol.handle_json('{"version":1,"request_id":"req-1"}')
    )

    assert_error(response, ERROR_INVALID_REQUEST)


def test_encode_json_response_is_compact_and_deterministic():
    encoded = encode_json_response({"b": 2, "a": 1})

    assert encoded == b'{"a":1,"b":2}'


def test_build_error_response_uses_protocol_error_envelope():
    response = build_error_response(
        None,
        "transport_error",
        "Transport failed.",
        details={"retryable": False},
    )

    assert response == {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": None,
        "ok": False,
        "error": {
            "code": "transport_error",
            "message": "Transport failed.",
            "retryable": False,
        },
    }


def test_unicode_content_round_trips():
    _state, protocol = make_protocol()

    response = parse_response(
        protocol.handle_json(
            '{"version":1,"request_id":"заявка","method":"routing.status"}'
        )
    )

    assert response["request_id"] == "заявка"


def test_success_response_contains_no_python_only_objects():
    _state, protocol = make_protocol(routing_section())

    response = parse_response(
        protocol.handle_json(
            '{"version":1,"request_id":"req-1","method":"routing.status"}'
        )
    )

    assert isinstance(response["result"]["zone_names"], list)
    assert isinstance(response["result"]["route_names"], list)
    assert isinstance(response["result"]["target_ids"], list)
    assert response["result"]["target_ids"] == ["udp:a"]
    assert all(
        isinstance(target_id, str)
        for target_id in response["result"]["target_ids"]
    )
