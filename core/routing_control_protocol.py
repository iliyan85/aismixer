"""Versioned JSON protocol for local runtime control requests.

The protocol is transport-neutral: sockets, CLIs, HTTP handlers, and future
peer transports should provide framing separately and delegate decoded messages
to this module. Version 1 validates request envelopes, delegates routing
operations to RoutingControlService, and pulls statistics from an explicitly
injected runtime provider. It never compiles routing tables in this layer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.metrics import (
    EgressMetricsSnapshot,
    InputTrafficMetricsSnapshot,
    OutputTrafficMetricsSnapshot,
    ProcessorMetricsSnapshot,
    QueueMetricsSnapshot,
    RuntimeStatisticsSnapshot,
)
from core.routing_control import (
    RoutingCandidateConfigError,
    RoutingControlService,
    RoutingControlStatus,
)
from core.routing_state import StaleRoutingGenerationError
from core.runtime_statistics import RuntimeStatisticsSource


ROUTING_CONTROL_PROTOCOL_VERSION = 1

ERROR_MALFORMED_JSON = "malformed_json"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_UNSUPPORTED_VERSION = "unsupported_version"
ERROR_UNKNOWN_METHOD = "unknown_method"
ERROR_INVALID_ROUTING_CONFIG = "invalid_routing_config"
ERROR_STALE_GENERATION = "stale_generation"

METHOD_STATUS = "routing.status"
METHOD_REPLACE = "routing.replace"
METHOD_DISABLE = "routing.disable"
METHOD_RUNTIME_STATISTICS = "runtime.statistics"
METHOD_RUNTIME_STATISTICS_INPUTS = "runtime.statistics.inputs"
METHOD_RUNTIME_STATISTICS_OUTPUTS = "runtime.statistics.outputs"


class MalformedJsonError(ValueError):
    """Raised when raw JSON cannot be trusted as a request object."""


@dataclass(frozen=True, slots=True)
class _RequestError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class _ValidatedRequest:
    request_id: str
    method: str
    params: Mapping[str, object] | None = None


def decode_json_request(data: bytes | str) -> Mapping[str, object]:
    """Decode UTF-8 JSON request data and require an object root."""

    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedJsonError("Malformed JSON request.") from exc
    elif isinstance(data, str):
        text = data
    else:
        raise TypeError("JSON request data must be bytes or str.")

    try:
        request = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedJsonError("Malformed JSON request.") from exc

    if not isinstance(request, Mapping):
        raise MalformedJsonError("Malformed JSON request.")
    return request


def encode_json_response(response: Mapping[str, object]) -> bytes:
    """Encode a JSON response as compact deterministic UTF-8 bytes."""

    return json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_error_response(
    request_id: str | None,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a stable routing-control error response envelope."""

    return _error_response(request_id, code, message, details=details)


class RoutingControlProtocol:
    """Validate JSON control requests and delegate to RoutingControlService.

    This class deliberately omits transport framing. Callers can use it behind
    Unix sockets, HTTP, CLIs, or IPC without giving those transports authority
    over routing compilation, target validation, or generation ownership.
    """

    def __init__(
        self,
        service: RoutingControlService,
        statistics_provider: RuntimeStatisticsSource,
    ):
        self._service = service
        self._statistics_provider = statistics_provider

    def handle_json(self, data: bytes | str) -> bytes:
        """Handle one unframed JSON message and return response bytes."""

        try:
            request = decode_json_request(data)
        except MalformedJsonError:
            return encode_json_response(
                _error_response(
                    None,
                    ERROR_MALFORMED_JSON,
                    "Malformed JSON request.",
                )
            )

        return encode_json_response(self.handle_request(request))

    def handle_request(self, request: Mapping[str, object]) -> dict[str, object]:
        """Handle one validated mapping-shaped protocol request."""

        validation_error = _validate_request_schema(request)
        request_id = _valid_request_id_or_none(request)
        if validation_error is not None:
            return _error_response(
                request_id,
                validation_error.code,
                validation_error.message,
            )

        validated = _coerce_validated_request(request)

        if validated.method == METHOD_RUNTIME_STATISTICS:
            snapshot = self._statistics_provider.snapshot()
            return _success_response(
                validated.request_id,
                _runtime_statistics_result(snapshot),
            )

        if validated.method == METHOD_RUNTIME_STATISTICS_INPUTS:
            snapshots = self._statistics_provider.input_traffic_snapshot()
            params = validated.params or {}
            input_name = params.get("input")
            assert input_name is None or isinstance(input_name, str)
            return _success_response(
                validated.request_id,
                _runtime_statistics_inputs_result(
                    snapshots,
                    input_name=input_name,
                ),
            )

        if validated.method == METHOD_RUNTIME_STATISTICS_OUTPUTS:
            snapshots = self._statistics_provider.output_traffic_snapshot()
            params = validated.params or {}
            target_id = params.get("target_id")
            name = params.get("name")
            assert target_id is None or isinstance(target_id, int)
            assert name is None or isinstance(name, str)
            return _success_response(
                validated.request_id,
                _runtime_statistics_outputs_result(
                    snapshots,
                    target_id=target_id,
                    name=name,
                ),
            )

        if validated.method == METHOD_STATUS:
            status = self._service.status()
            return _success_response(
                validated.request_id,
                _status_result(status),
            )

        if validated.method == METHOD_REPLACE:
            params = validated.params
            assert params is not None
            try:
                status = self._service.replace_from_config(
                    params["routing"],
                    expected_generation=params.get("expected_generation"),
                )
            except StaleRoutingGenerationError as exc:
                return _stale_generation_response(validated.request_id, exc)
            except RoutingCandidateConfigError as exc:
                return _invalid_routing_config_response(validated.request_id, exc)

            return _success_response(
                validated.request_id,
                _status_result(status),
            )

        if validated.method == METHOD_DISABLE:
            params = validated.params or {}
            try:
                status = self._service.disable(
                    expected_generation=params.get("expected_generation"),
                )
            except StaleRoutingGenerationError as exc:
                return _stale_generation_response(validated.request_id, exc)

            return _success_response(
                validated.request_id,
                _status_result(status),
            )

        raise AssertionError(f"Unsupported validated method: {validated.method}")


def _validate_request_schema(request: Mapping[str, object]) -> _RequestError | None:
    if not isinstance(request, Mapping):
        return _RequestError(ERROR_INVALID_REQUEST, "Request must be an object.")

    allowed_fields = {"version", "request_id", "method", "params"}
    unknown_fields = set(request) - allowed_fields
    if unknown_fields:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Request has unknown field(s): "
            f"{', '.join(sorted(str(field) for field in unknown_fields))}.",
        )

    for field_name in ("version", "request_id", "method"):
        if field_name not in request:
            return _RequestError(
                ERROR_INVALID_REQUEST,
                f"Request is missing required field {field_name!r}.",
            )

    version = request["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Request field 'version' must be an integer.",
        )
    if version != ROUTING_CONTROL_PROTOCOL_VERSION:
        return _RequestError(
            ERROR_UNSUPPORTED_VERSION,
            f"Unsupported routing control protocol version: {version}.",
        )

    request_id = request["request_id"]
    if not isinstance(request_id, str) or not request_id:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Request field 'request_id' must be a non-empty string.",
        )

    method = request["method"]
    if not isinstance(method, str) or not method:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Request field 'method' must be a non-empty string.",
        )
    if method not in {
        METHOD_STATUS,
        METHOD_REPLACE,
        METHOD_DISABLE,
        METHOD_RUNTIME_STATISTICS,
        METHOD_RUNTIME_STATISTICS_INPUTS,
        METHOD_RUNTIME_STATISTICS_OUTPUTS,
    }:
        return _RequestError(
            ERROR_UNKNOWN_METHOD,
            f"Unknown routing control method: {method}.",
        )

    if method in {METHOD_STATUS, METHOD_RUNTIME_STATISTICS}:
        if "params" in request:
            return _RequestError(
                ERROR_INVALID_REQUEST,
                f"Method {method!r} does not accept params.",
            )
        return None

    if method == METHOD_RUNTIME_STATISTICS_INPUTS:
        return _validate_runtime_statistics_inputs_params(request)

    if method == METHOD_RUNTIME_STATISTICS_OUTPUTS:
        return _validate_runtime_statistics_outputs_params(request)

    if method == METHOD_REPLACE:
        if "params" not in request:
            return _RequestError(
                ERROR_INVALID_REQUEST,
                "Method 'routing.replace' requires params.",
            )
        return _validate_replace_params(request["params"])

    if "params" in request:
        params = request["params"]
        if not isinstance(params, Mapping):
            return _RequestError(
                ERROR_INVALID_REQUEST,
                "Method 'routing.disable' params must be an object.",
            )
        return _validate_params_fields(
            params,
            allowed_fields={"expected_generation"},
            method=METHOD_DISABLE,
        )
    return None


def _validate_runtime_statistics_inputs_params(
    request: Mapping[str, object],
) -> _RequestError | None:
    if "params" not in request:
        return None
    params = request["params"]
    if not isinstance(params, Mapping):
        return _RequestError(
            ERROR_INVALID_REQUEST,
            f"Method {METHOD_RUNTIME_STATISTICS_INPUTS!r} params must be an object.",
        )

    error = _validate_params_fields(
        params,
        allowed_fields={"input"},
        method=METHOD_RUNTIME_STATISTICS_INPUTS,
    )
    if error is not None:
        return error

    if "input" in params:
        input_name = params["input"]
        if not isinstance(input_name, str) or not input_name:
            return _RequestError(
                ERROR_INVALID_REQUEST,
                "Param 'input' must be a non-empty string.",
            )
    return None


def _validate_runtime_statistics_outputs_params(
    request: Mapping[str, object],
) -> _RequestError | None:
    if "params" not in request:
        return None
    params = request["params"]
    if not isinstance(params, Mapping):
        return _RequestError(
            ERROR_INVALID_REQUEST,
            f"Method {METHOD_RUNTIME_STATISTICS_OUTPUTS!r} params must be an object.",
        )

    error = _validate_params_fields(
        params,
        allowed_fields={"target_id", "name"},
        method=METHOD_RUNTIME_STATISTICS_OUTPUTS,
    )
    if error is not None:
        return error

    if "target_id" in params and "name" in params:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            f"Method {METHOD_RUNTIME_STATISTICS_OUTPUTS!r} params must not "
            "contain both 'target_id' and 'name'.",
        )

    if "target_id" in params:
        target_id = params["target_id"]
        if (
            isinstance(target_id, bool)
            or not isinstance(target_id, int)
            or target_id < 0
        ):
            return _RequestError(
                ERROR_INVALID_REQUEST,
                "Param 'target_id' must be a non-negative integer.",
            )

    if "name" in params:
        name = params["name"]
        if not isinstance(name, str) or not name:
            return _RequestError(
                ERROR_INVALID_REQUEST,
                "Param 'name' must be a non-empty string.",
            )
    return None


def _validate_replace_params(params: object) -> _RequestError | None:
    if not isinstance(params, Mapping):
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Method 'routing.replace' params must be an object.",
        )

    error = _validate_params_fields(
        params,
        allowed_fields={"routing", "expected_generation"},
        method=METHOD_REPLACE,
    )
    if error is not None:
        return error

    if "routing" not in params:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Method 'routing.replace' params missing required field 'routing'.",
        )
    return None


def _validate_params_fields(
    params: Mapping[str, object],
    allowed_fields: set[str],
    method: str,
) -> _RequestError | None:
    unknown_fields = set(params) - allowed_fields
    if unknown_fields:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            f"Method {method!r} params has unknown field(s): "
            f"{', '.join(sorted(str(field) for field in unknown_fields))}.",
        )

    if "expected_generation" in params:
        return _validate_expected_generation(params["expected_generation"])
    return None


def _validate_expected_generation(value: object) -> _RequestError | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Param 'expected_generation' must be a non-negative integer.",
        )
    if value < 0:
        return _RequestError(
            ERROR_INVALID_REQUEST,
            "Param 'expected_generation' must be a non-negative integer.",
        )
    return None


def _coerce_validated_request(
    request: Mapping[str, object],
) -> _ValidatedRequest:
    params = request.get("params")
    if params is not None:
        assert isinstance(params, Mapping)
    return _ValidatedRequest(
        request_id=request["request_id"],
        method=request["method"],
        params=params,
    )


def _success_response(
    request_id: str,
    result: Mapping[str, object],
) -> dict[str, object]:
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": dict(result),
    }


def _error_response(
    request_id: str | None,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code,
        "message": message,
    }
    if details is not None:
        error.update(details)
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": error,
    }


def _stale_generation_response(
    request_id: str,
    exc: StaleRoutingGenerationError,
) -> dict[str, object]:
    return _error_response(
        request_id,
        ERROR_STALE_GENERATION,
        str(exc),
        details={
            "expected_generation": exc.expected_generation,
            "actual_generation": exc.actual_generation,
        },
    )


def _invalid_routing_config_response(
    request_id: str,
    exc: Exception,
) -> dict[str, object]:
    return _error_response(
        request_id,
        ERROR_INVALID_ROUTING_CONFIG,
        str(exc),
    )


def _status_result(status: RoutingControlStatus) -> dict[str, object]:
    return {
        "generation": status.generation,
        "enabled": status.enabled,
        "zone_names": list(status.zone_names),
        "route_names": list(status.route_names),
        "target_ids": list(status.target_ids),
    }


def _runtime_statistics_result(
    snapshot: RuntimeStatisticsSnapshot,
) -> dict[str, object]:
    """Serialize the stable public result without dataclass introspection."""

    if not isinstance(snapshot, RuntimeStatisticsSnapshot):
        raise TypeError(
            "statistics provider must return a RuntimeStatisticsSnapshot."
        )
    return {
        "ingress_queues": [
            _queue_metrics_result(queue) for queue in snapshot.ingress_queues
        ],
        "processing_queue": _queue_metrics_result(snapshot.processing_queue),
        "processor": _processor_metrics_result(snapshot.processor),
        "egress_queue": _queue_metrics_result(snapshot.egress_queue),
        "egress_operations": _egress_metrics_result(
            snapshot.egress_operations
        ),
    }


def _runtime_statistics_inputs_result(
    snapshots: tuple[InputTrafficMetricsSnapshot, ...],
    *,
    input_name: str | None,
) -> dict[str, object]:
    rows = tuple(_input_traffic_metrics_result(snapshot) for snapshot in snapshots)
    return {
        "inputs": [
            row
            for row in rows
            if input_name is None or row["name"] == input_name
        ]
    }


def _input_traffic_metrics_result(
    snapshot: InputTrafficMetricsSnapshot,
) -> dict[str, object]:
    if not isinstance(snapshot, InputTrafficMetricsSnapshot):
        raise TypeError(
            "statistics provider must return InputTrafficMetricsSnapshot "
            "instances."
        )
    return {
        "name": snapshot.name,
        "kind": snapshot.kind,
        "transport_packets": snapshot.transport_packets,
        "transport_bytes": snapshot.transport_bytes,
        "accepted_frames": snapshot.accepted_frames,
        "payload_bytes": snapshot.payload_bytes,
    }


def _runtime_statistics_outputs_result(
    snapshots: tuple[OutputTrafficMetricsSnapshot, ...],
    *,
    target_id: int | None,
    name: str | None,
) -> dict[str, object]:
    rows = tuple(_output_traffic_metrics_result(snapshot) for snapshot in snapshots)
    if target_id is not None:
        rows = tuple(row for row in rows if row["target_id"] == target_id)
    elif name is not None:
        rows = tuple(row for row in rows if row["name"] == name)
    return {"outputs": list(rows)}


def _output_traffic_metrics_result(
    snapshot: OutputTrafficMetricsSnapshot,
) -> dict[str, object]:
    if not isinstance(snapshot, OutputTrafficMetricsSnapshot):
        raise TypeError(
            "statistics provider must return OutputTrafficMetricsSnapshot "
            "instances."
        )
    # Completion records only a successful local dispatch operation. UDP does
    # not acknowledge remote receipt or downstream processing.
    return {
        "target_id": snapshot.target_id,
        "name": snapshot.name,
        "dispatch_attempts": snapshot.dispatch_attempts,
        "dispatch_completed": snapshot.dispatch_completed,
        "dispatch_failed": snapshot.dispatch_failed,
        "messages": snapshot.messages,
        "bytes": snapshot.bytes,
    }


def _queue_metrics_result(snapshot: QueueMetricsSnapshot) -> dict[str, object]:
    return {
        "name": snapshot.name,
        "capacity": snapshot.capacity,
        "depth": snapshot.depth,
        "peak_depth": snapshot.peak_depth,
        "enqueued": snapshot.enqueued,
        "dequeued": snapshot.dequeued,
        "put_waits": snapshot.put_waits,
        "current_put_waiters": snapshot.current_put_waiters,
    }


def _processor_metrics_result(
    snapshot: ProcessorMetricsSnapshot,
) -> dict[str, object]:
    return {
        "process_calls": snapshot.process_calls,
        "process_completed": snapshot.process_completed,
        "process_failed": snapshot.process_failed,
        "process_in_flight": snapshot.process_in_flight,
        "outputless_calls": snapshot.outputless_calls,
        "output_batches": snapshot.output_batches,
        "output_messages": snapshot.output_messages,
        "reset_calls": snapshot.reset_calls,
        "reset_completed": snapshot.reset_completed,
        "reset_failed": snapshot.reset_failed,
        "reset_in_flight": snapshot.reset_in_flight,
    }


def _egress_metrics_result(
    snapshot: EgressMetricsSnapshot,
) -> dict[str, object]:
    # These are local egress operations, not remote-delivery confirmations.
    return {
        "batches_started": snapshot.batches_started,
        "batches_completed": snapshot.batches_completed,
        "batches_failed": snapshot.batches_failed,
        "batches_cancelled": snapshot.batches_cancelled,
        "active_batches": snapshot.active_batches,
        "outputs_started": snapshot.outputs_started,
        "outputs_completed": snapshot.outputs_completed,
        "outputs_failed": snapshot.outputs_failed,
        "outputs_cancelled": snapshot.outputs_cancelled,
        "active_outputs": snapshot.active_outputs,
    }


def _valid_request_id_or_none(request: object) -> str | None:
    if not isinstance(request, Mapping):
        return None
    request_id: Any = request.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    return None
