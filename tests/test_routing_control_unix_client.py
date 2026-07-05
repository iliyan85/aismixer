import asyncio
import json
import socket

import pytest

from core.routing_control import RoutingControlService
from core.routing_control_protocol import ROUTING_CONTROL_PROTOCOL_VERSION
from core.routing_control_unix_client import (
    RoutingControlClientError,
    RoutingControlConnectionError,
    RoutingControlResponseError,
    RoutingControlResponseTooLargeError,
    RoutingControlUnixClient,
)
from core.routing_control_protocol import RoutingControlProtocol
from core.routing_control_unix import RoutingControlUnixServer
from core.routing_state import RoutingState


HAS_UNIX_SOCKETS = (
    hasattr(socket, "AF_UNIX")
    and hasattr(asyncio, "start_unix_server")
    and hasattr(asyncio, "open_unix_connection")
)
unix_socket_test = pytest.mark.skipif(
    not HAS_UNIX_SOCKETS,
    reason="Unix-domain asyncio sockets are not supported on this platform.",
)


def run(coro):
    return asyncio.run(coro)


def request_mapping(request_id="req-1"):
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "method": "routing.status",
    }


def response_mapping(request_id="req-1", *, ok=True):
    if ok:
        return {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": True,
            "result": {"enabled": False},
        }
    return {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {"code": "invalid_request", "message": "Invalid request."},
    }


def encode_frame(mapping, *, newline=True):
    data = json.dumps(
        mapping,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return data + (b"\n" if newline else b"")


class FakeWriter:
    def __init__(self, *, close_exc=None, wait_closed_exc=None):
        self.write_calls = []
        self.drain_count = 0
        self.close_count = 0
        self.wait_closed_count = 0
        self.close_exc = close_exc
        self.wait_closed_exc = wait_closed_exc

    def write(self, data):
        self.write_calls.append(bytes(data))

    async def drain(self):
        self.drain_count += 1

    def close(self):
        self.close_count += 1
        if self.close_exc is not None:
            raise self.close_exc

    async def wait_closed(self):
        self.wait_closed_count += 1
        if self.wait_closed_exc is not None:
            raise self.wait_closed_exc


class RaisingReader:
    def __init__(self, exc):
        self.exc = exc

    async def read(self, _size):
        raise self.exc


async def request_with_response(
    monkeypatch,
    response_bytes,
    *,
    request=None,
    max_response_bytes=1024,
    writer=None,
    reader=None,
):
    if reader is None:
        reader = asyncio.StreamReader()
        reader.feed_data(response_bytes)
        reader.feed_eof()
    writer = writer or FakeWriter()

    async def fake_open_unix_connection(path):
        assert path == "control.sock"
        return reader, writer

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
        raising=False,
    )

    client = RoutingControlUnixClient(
        "control.sock",
        max_response_bytes=max_response_bytes,
    )
    response = await client.request(request or request_mapping())
    return response, writer


def test_constructor_rejects_invalid_socket_path():
    with pytest.raises(ValueError, match="socket_path"):
        RoutingControlUnixClient("")
    with pytest.raises(TypeError, match="socket_path"):
        RoutingControlUnixClient(b"control.sock")


@pytest.mark.parametrize(
    ("max_response_bytes", "error"),
    [(True, TypeError), (0, ValueError)],
)
def test_constructor_rejects_invalid_response_limit(max_response_bytes, error):
    with pytest.raises(error):
        RoutingControlUnixClient(
            "control.sock",
            max_response_bytes=max_response_bytes,
        )


def test_request_is_encoded_as_one_newline_terminated_json_frame(monkeypatch):
    response, writer = run(
        request_with_response(monkeypatch, encode_frame(response_mapping()))
    )

    assert response["ok"] is True
    assert writer.write_calls == [encode_frame(request_mapping())]
    assert not writer.write_calls[0].endswith(b"\n\n")


def test_writer_drain_and_close_are_awaited(monkeypatch):
    _response, writer = run(
        request_with_response(monkeypatch, encode_frame(response_mapping()))
    )

    assert writer.drain_count == 1
    assert writer.close_count == 1
    assert writer.wait_closed_count == 1


def test_successful_response_is_decoded(monkeypatch):
    response, _writer = run(
        request_with_response(monkeypatch, encode_frame(response_mapping()))
    )

    assert response == response_mapping()


def test_final_eof_terminated_response_without_newline_is_accepted(monkeypatch):
    response, _writer = run(
        request_with_response(
            monkeypatch,
            encode_frame(response_mapping("req-1"), newline=False),
        )
    )

    assert response["request_id"] == "req-1"


@pytest.mark.parametrize("response_bytes", [b"", b"\n"])
def test_empty_or_blank_response_is_rejected(monkeypatch, response_bytes):
    with pytest.raises(RoutingControlResponseError):
        run(request_with_response(monkeypatch, response_bytes))


@pytest.mark.parametrize("response_bytes", [b"{\n", b"[]\n"])
def test_malformed_or_non_object_json_response_is_rejected(monkeypatch, response_bytes):
    with pytest.raises(RoutingControlResponseError):
        run(request_with_response(monkeypatch, response_bytes))


@pytest.mark.parametrize(
    "response",
    [
        {**response_mapping(), "version": 2},
        {**response_mapping(), "version": True},
        {**response_mapping(), "request_id": "other"},
        {**response_mapping(), "request_id": None},
        {**response_mapping(), "ok": "yes"},
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": True,
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": True,
            "result": {},
            "error": {"code": "bad", "message": "bad"},
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": True,
            "result": [],
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": False,
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": False,
            "error": {"code": "bad", "message": "bad"},
            "result": {},
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": False,
            "error": {},
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": False,
            "error": {"code": "", "message": "bad"},
        },
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "req-1",
            "ok": False,
            "error": {"code": "bad", "message": 7},
        },
        {**response_mapping(), "extra": True},
    ],
)
def test_invalid_response_envelopes_are_rejected(monkeypatch, response):
    with pytest.raises(RoutingControlResponseError):
        run(request_with_response(monkeypatch, encode_frame(response)))


def test_null_response_id_is_allowed_when_request_has_no_trusted_id(monkeypatch):
    request = {
        "version": ROUTING_CONTROL_PROTOCOL_VERSION,
        "request_id": "",
        "method": "routing.status",
    }
    response, _writer = run(
        request_with_response(
            monkeypatch,
            encode_frame(response_mapping(None)),
            request=request,
        )
    )

    assert response["request_id"] is None


def test_oversized_response_is_rejected_without_content_in_exception(monkeypatch):
    with pytest.raises(RoutingControlResponseTooLargeError) as exc_info:
        run(
            request_with_response(
                monkeypatch,
                b"abcd\n",
                max_response_bytes=3,
            )
        )

    assert "abcd" not in str(exc_info.value)


def test_connection_failures_are_wrapped(monkeypatch):
    async def fake_open_unix_connection(_path):
        raise FileNotFoundError("missing socket")

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
        raising=False,
    )
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(RoutingControlConnectionError) as exc_info:
        run(client.request(request_mapping()))

    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_unimplemented_unix_connection_api_is_wrapped(monkeypatch):
    async def fake_open_unix_connection(_path):
        raise NotImplementedError("not available")

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
        raising=False,
    )
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(RoutingControlConnectionError):
        run(client.request(request_mapping()))


def test_unavailable_unix_connection_api_is_a_connection_error(monkeypatch):
    monkeypatch.setattr(asyncio, "open_unix_connection", None, raising=False)
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(RoutingControlConnectionError):
        run(client.request(request_mapping()))


def test_connection_reset_before_complete_response_is_wrapped(monkeypatch):
    writer = FakeWriter()

    async def fake_open_unix_connection(_path):
        return RaisingReader(ConnectionResetError("reset")), writer

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
        raising=False,
    )
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(RoutingControlConnectionError):
        run(client.request(request_mapping()))

    assert writer.close_count == 1


def test_cancellation_propagates(monkeypatch):
    writer = FakeWriter()

    async def fake_open_unix_connection(_path):
        return RaisingReader(asyncio.CancelledError()), writer

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
        raising=False,
    )
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(asyncio.CancelledError):
        run(client.request(request_mapping()))

    assert writer.close_count == 1


def test_disconnect_while_closing_is_tolerated(monkeypatch):
    writer = FakeWriter(
        close_exc=BrokenPipeError("closed"),
        wait_closed_exc=ConnectionResetError("reset"),
    )

    response, writer = run(
        request_with_response(
            monkeypatch,
            encode_frame(response_mapping()),
            writer=writer,
        )
    )

    assert response["ok"] is True
    assert writer.close_count == 1


def test_non_mapping_request_is_rejected(monkeypatch):
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(TypeError, match="mapping"):
        run(client.request([]))


def test_unencodable_request_is_client_error(monkeypatch):
    async def fake_open_unix_connection(_path):
        raise AssertionError("connection must not be opened")

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
        raising=False,
    )
    client = RoutingControlUnixClient("control.sock")

    with pytest.raises(RoutingControlClientError):
        run(client.request({"request_id": object()}))


def routing_section():
    return {
        "zones": {"source": {"include": ["udp:source"]}},
        "routes": [
            {
                "name": "source_to_a",
                "from_zone": "source",
                "to": ["udp:a"],
            }
        ],
    }


def make_control_protocol():
    service = RoutingControlService(RoutingState(), ("udp:a",))
    return RoutingControlProtocol(service)


@unix_socket_test
def test_unix_client_server_round_trips(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_control_protocol(), path)
        await server.start()
        client = RoutingControlUnixClient(path)
        try:
            status = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "status-1",
                    "method": "routing.status",
                }
            )
            replace = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "replace-1",
                    "method": "routing.replace",
                    "params": {"routing": routing_section()},
                }
            )
            disable = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "disable-1",
                    "method": "routing.disable",
                    "params": {"expected_generation": 1},
                }
            )
            stale = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "stale-1",
                    "method": "routing.disable",
                    "params": {"expected_generation": 1},
                }
            )
        finally:
            await server.close()

        assert status["ok"] is True
        assert status["request_id"] == "status-1"
        assert replace["ok"] is True
        assert replace["request_id"] == "replace-1"
        assert replace["result"]["generation"] == 1
        assert disable["ok"] is True
        assert disable["request_id"] == "disable-1"
        assert disable["result"]["generation"] == 2
        assert stale["ok"] is False
        assert stale["request_id"] == "stale-1"
        assert stale["error"]["code"] == "stale_generation"

    run(scenario())
