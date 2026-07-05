import json

import pytest

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
    ROUTING_CONTROL_PROTOCOL_VERSION,
    RoutingControlProtocol,
    build_error_response,
    decode_json_request,
    encode_json_response,
)
from core.routing_state import RoutingState
from core.runtime_routing import compile_routing_section


AVAILABLE_TARGETS = ("udp:a", "udp:b", "udp:c")


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
        initial_table = compile_routing_section(initial_section, AVAILABLE_TARGETS)
    state = RoutingState(initial_table)
    return state, RoutingControlService(state, AVAILABLE_TARGETS)


def make_protocol(initial_section=None):
    state, service = make_service(initial_section)
    return state, RoutingControlProtocol(service)


def status_request(request_id="req-1"):
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": "routing.status",
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

    protocol = RoutingControlProtocol(BrokenService())

    with pytest.raises(type(exception), match="programming defect"):
        protocol.handle_request(replace_request())


def test_candidate_config_error_maps_to_invalid_routing_config():
    class InvalidCandidateService:
        def replace_from_config(self, routing_config, expected_generation=None):
            raise RoutingCandidateConfigError("invalid candidate")

    protocol = RoutingControlProtocol(InvalidCandidateService())

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

    protocol = RoutingControlProtocol(ReplaceOnlyService())

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

    protocol = RoutingControlProtocol(DisableOnlyService())

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
