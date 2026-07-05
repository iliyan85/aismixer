"""One-request Unix-domain NDJSON client for routing control.

RoutingControlUnixClient opens one local Unix-domain socket connection per
request, sends one compact JSON request followed by a newline, reads one
newline-delimited response, and validates the minimum version-1 response
envelope. Response request IDs are correlated with the request before the
decoded mapping is returned.

This module is a transport client only. Routing validation, generation checks,
and routing table installation remain server-side responsibilities.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Mapping
from typing import Final, Any

from core.routing_control_protocol import ROUTING_CONTROL_PROTOCOL_VERSION


_READ_CHUNK_SIZE: Final = 65_536


class RoutingControlClientError(RuntimeError):
    """Base class for Unix routing-control client failures."""


class RoutingControlConnectionError(RoutingControlClientError):
    """Raised when the Unix socket connection cannot be used."""


class RoutingControlResponseError(RoutingControlClientError):
    """Raised when a routing-control response cannot be trusted."""


class RoutingControlResponseTooLargeError(RoutingControlResponseError):
    """Raised when a routing-control response exceeds the configured limit."""


class RoutingControlUnixClient:
    """Async Unix-domain NDJSON client for routing-control requests.

    Each request uses a fresh local Unix socket connection and expects exactly
    one response frame. The client validates response shape and request ID
    correlation but does not interpret routing configuration semantics.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        max_response_bytes: int = 1_048_576,
    ):
        try:
            path = os.fspath(socket_path)
        except TypeError as exc:
            raise TypeError("socket_path must be a filesystem path.") from exc
        if not isinstance(path, str):
            raise TypeError("socket_path must be a string filesystem path.")
        if not path:
            raise ValueError("socket_path must be non-empty.")

        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise TypeError("max_response_bytes must be a positive integer.")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer.")

        self._socket_path = path
        self._max_response_bytes = max_response_bytes

    async def request(self, request: Mapping[str, object]) -> Mapping[str, object]:
        """Send one request and return one validated response mapping."""

        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping.")

        open_unix_connection = getattr(asyncio, "open_unix_connection", None)
        if open_unix_connection is None:
            raise RoutingControlConnectionError(
                "Unix-domain routing control is not supported on this platform."
            )

        try:
            payload = _encode_json_request(request)
        except (TypeError, ValueError) as exc:
            raise RoutingControlClientError(
                "Routing control request could not be JSON encoded."
            ) from exc

        writer = None
        try:
            reader, writer = await open_unix_connection(self._socket_path)
            writer.write(payload + b"\n")
            await writer.drain()
            frame = await _read_response_frame(reader, self._max_response_bytes)
            response = _decode_response(frame)
            _validate_response_envelope(response)
            _validate_response_correlation(request, response)
            return response
        except asyncio.CancelledError:
            raise
        except RoutingControlResponseError:
            raise
        except (BrokenPipeError, ConnectionError, OSError, NotImplementedError) as exc:
            raise RoutingControlConnectionError(
                "Routing control Unix socket connection failed."
            ) from exc
        finally:
            if writer is not None:
                await _close_writer(writer)


def _encode_json_request(request: Mapping[str, object]) -> bytes:
    return json.dumps(
        request,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


async def _read_response_frame(reader, max_response_bytes: int) -> bytes:
    buffer = b""

    while True:
        newline_at = buffer.find(b"\n")
        if newline_at >= 0:
            frame = buffer[:newline_at]
            if not frame:
                raise RoutingControlResponseError("Empty routing control response frame.")
            if len(frame) > max_response_bytes:
                raise RoutingControlResponseTooLargeError(
                    "Routing control response exceeds maximum frame size."
                )
            return frame

        if len(buffer) > max_response_bytes:
            raise RoutingControlResponseTooLargeError(
                "Routing control response exceeds maximum frame size."
            )

        read_size = min(_READ_CHUNK_SIZE, max_response_bytes + 1 - len(buffer))
        if read_size <= 0:
            read_size = 1

        try:
            chunk = await reader.read(read_size)
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionError, OSError, NotImplementedError) as exc:
            raise RoutingControlConnectionError(
                "Routing control Unix socket connection failed."
            ) from exc

        if not chunk:
            if not buffer:
                raise RoutingControlResponseError("Empty routing control response.")
            return buffer
        buffer += chunk


def _decode_response(frame: bytes) -> dict[str, object]:
    try:
        text = frame.decode("utf-8")
        response = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoutingControlResponseError("Malformed routing control response.") from exc

    if not isinstance(response, dict):
        raise RoutingControlResponseError("Routing control response must be an object.")
    return response


def _validate_response_envelope(response: Mapping[str, object]) -> None:
    version = response.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise RoutingControlResponseError("Routing control response version is invalid.")
    if version != ROUTING_CONTROL_PROTOCOL_VERSION:
        raise RoutingControlResponseError("Unsupported routing control response version.")

    request_id = response.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise RoutingControlResponseError("Routing control response request_id is invalid.")

    ok = response.get("ok")
    if not isinstance(ok, bool):
        raise RoutingControlResponseError("Routing control response ok field is invalid.")

    expected_fields = {"version", "request_id", "ok", "result" if ok else "error"}
    unknown_fields = set(response) - expected_fields
    if unknown_fields:
        raise RoutingControlResponseError(
            "Routing control response has unknown top-level field(s)."
        )
    missing_fields = expected_fields - set(response)
    if missing_fields:
        raise RoutingControlResponseError(
            "Routing control response is missing required field(s)."
        )

    if ok:
        if not isinstance(response["result"], Mapping):
            raise RoutingControlResponseError(
                "Successful routing control response result must be an object."
            )
        return

    error = response["error"]
    if not isinstance(error, Mapping):
        raise RoutingControlResponseError(
            "Failed routing control response error must be an object."
        )

    code: Any = error.get("code")
    if not isinstance(code, str) or not code:
        raise RoutingControlResponseError(
            "Failed routing control response error code is invalid."
        )

    message: Any = error.get("message")
    if not isinstance(message, str):
        raise RoutingControlResponseError(
            "Failed routing control response error message is invalid."
        )


def _validate_response_correlation(
    request: Mapping[str, object],
    response: Mapping[str, object],
) -> None:
    expected_request_id = _trusted_request_id_or_none(request)
    if response["request_id"] != expected_request_id:
        raise RoutingControlResponseError(
            "Routing control response request_id does not match request."
        )


def _trusted_request_id_or_none(request: Mapping[str, object]) -> str | None:
    request_id: Any = request.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    return None


async def _close_writer(writer) -> None:
    try:
        writer.close()
    except (BrokenPipeError, ConnectionError):
        return

    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return

    try:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result
    except (BrokenPipeError, ConnectionError):
        return
