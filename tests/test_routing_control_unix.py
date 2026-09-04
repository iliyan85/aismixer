import asyncio
import json
import os
import socket
import stat

import pytest

from core.routing_control import RoutingControlService
from core.routing_control_protocol import (
    ERROR_MALFORMED_JSON,
    ROUTING_CONTROL_PROTOCOL_VERSION,
    RoutingControlProtocol,
    encode_json_response,
)
from core.routing_control_unix import (
    ERROR_FRAME_TOO_LARGE,
    ERROR_INTERNAL_ERROR,
    ControlSocketPathError,
    RoutingControlUnixServer,
    _probe_unix_socket,
    _SocketLiveness,
)
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


def status_request(request_id="req-1"):
    return encode_json_response(
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "routing.status",
        }
    )


def parse_response(data):
    return json.loads(data.decode("utf-8"))


def parse_write(write):
    assert write.endswith(b"\n")
    assert not write.endswith(b"\n\n")
    return parse_response(write[:-1])


class UnusedStatisticsSource:
    def snapshot(self):
        raise AssertionError("statistics must not be pulled by routing tests")


def make_protocol():
    service = RoutingControlService(RoutingState(), {"udp:a": 0})
    return RoutingControlProtocol(service, UnusedStatisticsSource())


class RecordingProtocol(RoutingControlProtocol):
    def __init__(self, responses=None, exception=None):
        self.frames = []
        self.responses = list(responses or [])
        self.exception = exception

    def handle_json(self, data):
        self.frames.append(data)
        if self.exception is not None:
            raise self.exception
        if self.responses:
            response = self.responses.pop(0)
        else:
            response = {
                "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                "request_id": f"req-{len(self.frames)}",
                "ok": True,
                "result": {"payload": data.decode("utf-8", "replace")},
            }
        if isinstance(response, bytes):
            return response
        return encode_json_response(response)


class FakeWriter:
    def __init__(
        self,
        *,
        write_exc=None,
        drain_exc=None,
        close_exc=None,
        wait_closed_exc=None,
    ):
        self.write_exc = write_exc
        self.drain_exc = drain_exc
        self.close_exc = close_exc
        self.wait_closed_exc = wait_closed_exc
        self.write_calls = []
        self.drain_count = 0
        self.close_count = 0
        self.wait_closed_count = 0
        self.closed = False

    def write(self, data):
        if self.write_exc is not None:
            raise self.write_exc
        self.write_calls.append(bytes(data))

    async def drain(self):
        self.drain_count += 1
        if self.drain_exc is not None:
            raise self.drain_exc

    def close(self):
        self.close_count += 1
        self.closed = True
        if self.close_exc is not None:
            raise self.close_exc

    async def wait_closed(self):
        self.wait_closed_count += 1
        if self.wait_closed_exc is not None:
            raise self.wait_closed_exc


async def handle_bytes(
    data,
    *,
    protocol=None,
    max_request_bytes=1024,
    writer=None,
):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    writer = writer or FakeWriter()
    protocol = protocol or RecordingProtocol()
    server = RoutingControlUnixServer(
        protocol,
        "control.sock",
        max_request_bytes=max_request_bytes,
    )

    await server._handle_connection(reader, writer)
    return writer, protocol


def test_constructor_rejects_invalid_protocol():
    with pytest.raises(TypeError, match="RoutingControlProtocol"):
        RoutingControlUnixServer(object(), "control.sock")


@pytest.mark.parametrize("socket_path", ["", b"control.sock"])
def test_constructor_rejects_invalid_socket_path(socket_path):
    with pytest.raises((TypeError, ValueError), match="socket_path"):
        RoutingControlUnixServer(RecordingProtocol(), socket_path)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_request_bytes": True}, TypeError),
        ({"max_request_bytes": 0}, ValueError),
        ({"socket_mode": True}, TypeError),
        ({"socket_mode": 0o1000}, ValueError),
    ],
)
def test_constructor_rejects_invalid_limits_and_modes(kwargs, error):
    with pytest.raises(error):
        RoutingControlUnixServer(RecordingProtocol(), "control.sock", **kwargs)


def test_one_request_produces_one_newline_terminated_response():
    writer, _protocol = run(handle_bytes(status_request() + b"\n", protocol=make_protocol()))

    assert len(writer.write_calls) == 1
    response = parse_write(writer.write_calls[0])
    assert response["ok"] is True
    assert response["request_id"] == "req-1"


def test_multiple_requests_on_one_connection_preserve_order():
    writer, _protocol = run(
        handle_bytes(
            status_request("req-1") + b"\n" + status_request("req-2") + b"\n",
            protocol=make_protocol(),
        )
    )

    assert [parse_write(call)["request_id"] for call in writer.write_calls] == [
        "req-1",
        "req-2",
    ]


def test_blank_line_produces_malformed_json():
    writer, _protocol = run(handle_bytes(b"\n", protocol=make_protocol()))

    response = parse_write(writer.write_calls[0])
    assert response["ok"] is False
    assert response["request_id"] is None
    assert response["error"]["code"] == ERROR_MALFORMED_JSON


def test_malformed_json_does_not_close_connection_before_later_valid_request():
    writer, _protocol = run(
        handle_bytes(b"{\n" + status_request("req-ok") + b"\n", protocol=make_protocol())
    )

    first = parse_write(writer.write_calls[0])
    second = parse_write(writer.write_calls[1])
    assert first["error"]["code"] == ERROR_MALFORMED_JSON
    assert second["ok"] is True
    assert second["request_id"] == "req-ok"


def test_partial_frame_at_eof_is_discarded_without_dispatch_or_response():
    protocol = RecordingProtocol()

    writer, protocol = run(
        handle_bytes(status_request("req-eof"), protocol=protocol)
    )

    assert protocol.frames == []
    assert writer.write_calls == []
    assert writer.closed is True


def test_complete_frame_before_partial_eof_frame_executes_only_the_complete_frame():
    protocol = RecordingProtocol()

    writer, protocol = run(
        handle_bytes(
            status_request("first") + b"\n" + status_request("second"),
            protocol=protocol,
        )
    )

    assert protocol.frames == [status_request("first")]
    assert len(writer.write_calls) == 1
    assert parse_write(writer.write_calls[0])["request_id"] == "req-1"


def test_partial_replace_frame_at_eof_cannot_mutate_routing():
    service = RoutingControlService(RoutingState(), {"udp:a": 0})
    protocol = RoutingControlProtocol(service, UnusedStatisticsSource())
    replace_frame = encode_json_response(
        {
            "version": ROUTING_CONTROL_PROTOCOL_VERSION,
            "request_id": "replace-eof",
            "method": "routing.replace",
            "params": {
                "routing": {
                    "zones": {"source": {"include": ["udp:source"]}},
                    "routes": [
                        {
                            "name": "source_to_a",
                            "from_zone": "source",
                            "to": ["udp:a"],
                        }
                    ],
                }
            },
        }
    )

    # Frame delivered without a trailing newline, then EOF.
    writer, _protocol = run(handle_bytes(replace_frame, protocol=protocol))

    assert writer.write_calls == []
    assert service.status().generation == 0


def test_oversized_frame_produces_frame_too_large_without_calling_protocol():
    protocol = RecordingProtocol()

    writer, protocol = run(
        handle_bytes(
            b"abcd\n",
            protocol=protocol,
            max_request_bytes=3,
        )
    )

    response = parse_write(writer.write_calls[0])
    assert response["ok"] is False
    assert response["request_id"] is None
    assert response["error"]["code"] == ERROR_FRAME_TOO_LARGE
    assert protocol.frames == []


def test_oversized_frame_closes_connection_after_one_response():
    protocol = RecordingProtocol()

    writer, protocol = run(
        handle_bytes(
            b"abcd\n" + status_request("req-after") + b"\n",
            protocol=protocol,
            max_request_bytes=3,
        )
    )

    assert len(writer.write_calls) == 1
    assert parse_write(writer.write_calls[0])["error"]["code"] == ERROR_FRAME_TOO_LARGE
    assert protocol.frames == []
    assert writer.closed is True


@pytest.mark.parametrize(
    "exception",
    [
        TypeError("secret type detail"),
        ValueError("secret value detail"),
        RuntimeError("secret runtime detail"),
    ],
)
def test_unexpected_protocol_errors_become_internal_error(exception):
    writer, _protocol = run(
        handle_bytes(
            status_request() + b"\n",
            protocol=RecordingProtocol(exception=exception),
        )
    )

    raw_response = writer.write_calls[0]
    response = parse_write(raw_response)
    assert response["ok"] is False
    assert response["request_id"] is None
    assert response["error"]["code"] == ERROR_INTERNAL_ERROR
    assert b"secret" not in raw_response
    assert type(exception).__name__.encode("ascii") not in raw_response
    assert writer.closed is True


def test_internal_error_closes_only_affected_client():
    protocol = RecordingProtocol(exception=RuntimeError("first failed"))
    first_writer, _protocol = run(
        handle_bytes(status_request("req-first") + b"\n", protocol=protocol)
    )
    protocol.exception = None

    second_writer, _protocol = run(
        handle_bytes(status_request("req-second") + b"\n", protocol=protocol)
    )

    assert parse_write(first_writer.write_calls[0])["error"]["code"] == ERROR_INTERNAL_ERROR
    assert parse_write(second_writer.write_calls[0])["ok"] is True
    assert first_writer.closed is True
    assert second_writer.closed is True


def test_writer_drain_is_awaited_for_each_response():
    writer, _protocol = run(
        handle_bytes(
            status_request("req-1") + b"\n" + status_request("req-2") + b"\n",
            protocol=make_protocol(),
        )
    )

    assert writer.drain_count == 2


def test_writer_is_closed_after_connection_handler_finishes():
    writer, _protocol = run(handle_bytes(status_request() + b"\n", protocol=make_protocol()))

    assert writer.close_count == 1
    assert writer.wait_closed_count == 1
    assert writer.closed is True


def test_client_disconnect_during_drain_is_tolerated():
    writer = FakeWriter(drain_exc=ConnectionResetError("client reset"))

    writer, _protocol = run(
        handle_bytes(
            status_request() + b"\n",
            protocol=make_protocol(),
            writer=writer,
        )
    )

    assert writer.drain_count == 1
    assert writer.closed is True


def test_client_disconnect_during_writer_close_is_tolerated():
    writer = FakeWriter(close_exc=BrokenPipeError("client disconnected"))

    writer, _protocol = run(
        handle_bytes(
            status_request() + b"\n",
            protocol=make_protocol(),
            writer=writer,
        )
    )

    assert writer.close_count == 1


def test_serve_forever_requires_started_server():
    server = RoutingControlUnixServer(RecordingProtocol(), "control.sock")

    with pytest.raises(RuntimeError, match="not running"):
        run(server.serve_forever())


def test_cancellation_propagates():
    writer = FakeWriter()

    with pytest.raises(asyncio.CancelledError):
        run(
            handle_bytes(
                status_request() + b"\n",
                protocol=RecordingProtocol(exception=asyncio.CancelledError()),
                writer=writer,
            )
        )

    assert writer.closed is True


@unix_socket_test
def test_start_creates_unix_socket(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        try:
            assert server.is_running is True
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_configured_socket_mode_is_applied(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path, socket_mode=0o600)
        await server.start()
        try:
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
        finally:
            await server.close()

    run(scenario())


async def unix_request(path, payload):
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(payload + b"\n")
        await writer.drain()
        return await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()


@unix_socket_test
def test_status_request_works_over_unix_socket(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        try:
            response = parse_response(await unix_request(path, status_request()))
            assert response["ok"] is True
            assert response["request_id"] == "req-1"
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_one_unix_connection_can_make_multiple_requests(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                writer.write(status_request("req-1") + b"\n")
                writer.write(status_request("req-2") + b"\n")
                await writer.drain()
                first = parse_response(await reader.readline())
                second = parse_response(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()

            assert [first["request_id"], second["request_id"]] == ["req-1", "req-2"]
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_close_removes_socket_and_is_idempotent(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        assert path.exists()

        await server.close()
        assert not path.exists()
        await server.close()

    run(scenario())


@unix_socket_test
def test_close_does_not_remove_replaced_socket_path(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()

        path.unlink()
        path.write_text("replacement", encoding="utf-8")
        await server.close()

        assert path.read_text(encoding="utf-8") == "replacement"

    run(scenario())


@unix_socket_test
def test_chmod_failure_removes_socket_from_failed_start(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)

        def fail_chmod(_path, _mode):
            raise PermissionError("chmod denied")

        monkeypatch.setattr(os, "chmod", fail_chmod)

        with pytest.raises(PermissionError, match="chmod denied"):
            await server.start()

        assert server.is_running is False
        assert not path.exists()

    run(scenario())


@unix_socket_test
def test_stale_unix_socket_is_removed_before_bind(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(path))
        finally:
            stale.close()
        assert stat.S_ISSOCK(os.lstat(path).st_mode)

        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        try:
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_regular_file_at_socket_path_is_rejected_and_preserved(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        path.write_text("keep me", encoding="utf-8")
        server = RoutingControlUnixServer(make_protocol(), path)

        with pytest.raises(ControlSocketPathError, match="regular file"):
            await server.start()

        assert path.read_text(encoding="utf-8") == "keep me"

    run(scenario())


@unix_socket_test
def test_directory_at_socket_path_is_rejected_and_preserved(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        path.mkdir()
        server = RoutingControlUnixServer(make_protocol(), path)

        with pytest.raises(ControlSocketPathError, match="directory"):
            await server.start()

        assert path.is_dir()

    run(scenario())


@unix_socket_test
def test_symbolic_link_at_socket_path_is_rejected_and_preserved(tmp_path):
    async def scenario():
        target = tmp_path / "target"
        target.write_text("target", encoding="utf-8")
        path = tmp_path / "control.sock"
        path.symlink_to(target)
        server = RoutingControlUnixServer(make_protocol(), path)

        with pytest.raises(ControlSocketPathError, match="symbolic link"):
            await server.start()

        assert path.is_symlink()
        assert target.read_text(encoding="utf-8") == "target"

    run(scenario())


@unix_socket_test
def test_missing_parent_directory_is_rejected(tmp_path):
    async def scenario():
        path = tmp_path / "missing" / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)

        with pytest.raises(ControlSocketPathError, match="parent directory"):
            await server.start()

    run(scenario())


@unix_socket_test
def test_second_start_while_running_is_rejected(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await server.start()
        finally:
            await server.close()

    run(scenario())


class FirstCallBrokenProtocol(RoutingControlProtocol):
    def __init__(self):
        self.calls = 0

    def handle_json(self, data):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("secret listener detail")
        return encode_json_response(
            {
                "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                "request_id": "after-error",
                "ok": True,
                "result": {},
            }
        )


@unix_socket_test
def test_listener_survives_internal_client_error(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(FirstCallBrokenProtocol(), path)
        await server.start()
        try:
            first = parse_response(await unix_request(path, status_request("req-first")))
            second = parse_response(await unix_request(path, status_request("req-second")))
        finally:
            await server.close()

        assert first["error"]["code"] == ERROR_INTERNAL_ERROR
        assert "secret" not in json.dumps(first)
        assert second["ok"] is True
        assert second["request_id"] == "after-error"

    run(scenario())


# --- Point 9 C: existing Unix socket path — stale vs live ---


@unix_socket_test
def test_probe_classifies_missing_path_as_stale(tmp_path):
    assert (
        _probe_unix_socket(str(tmp_path / "absent.sock"))
        is _SocketLiveness.STALE
    )


@unix_socket_test
def test_probe_classifies_dead_socket_file_as_stale(tmp_path):
    path = tmp_path / "dead.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        dead.bind(str(path))
    finally:
        dead.close()

    assert _probe_unix_socket(str(path)) is _SocketLiveness.STALE


@unix_socket_test
def test_probe_classifies_listening_socket_as_live(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        await server.start()
        try:
            assert _probe_unix_socket(str(path)) is _SocketLiveness.LIVE
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_live_control_socket_is_preserved_and_remains_reachable(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        server_a = RoutingControlUnixServer(make_protocol(), path)
        await server_a.start()
        before = os.lstat(path)
        try:
            server_b = RoutingControlUnixServer(make_protocol(), path)
            with pytest.raises(
                ControlSocketPathError,
                match="live control socket",
            ):
                await server_b.start()
            assert server_b.is_running is False

            after = os.lstat(path)
            assert (after.st_dev, after.st_ino) == (
                before.st_dev,
                before.st_ino,
            )

            response = parse_response(
                await unix_request(path, status_request("after-collision"))
            )
            assert response["ok"] is True
            assert response["request_id"] == "after-collision"
        finally:
            await server_a.close()

    run(scenario())


@unix_socket_test
def test_uncertain_probe_result_refuses_startup_without_unlink(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        path = tmp_path / "control.sock"
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            dead.bind(str(path))
        finally:
            dead.close()
        before = os.lstat(path)

        monkeypatch.setattr(
            "core.routing_control_unix._probe_unix_socket",
            lambda _path: _SocketLiveness.UNCERTAIN,
        )

        server = RoutingControlUnixServer(make_protocol(), path)
        with pytest.raises(ControlSocketPathError, match="stale"):
            await server.start()

        after = os.lstat(path)
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)

    run(scenario())


@unix_socket_test
def test_live_socket_swapped_in_after_probe_is_not_unlinked(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        path = tmp_path / "control.sock"
        original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            original.bind(str(path))
        finally:
            original.close()

        import core.routing_control_unix as unix_module

        real_probe = unix_module._probe_unix_socket
        holder = {}

        def probe_then_swap_live(probe_path):
            if holder.get("swapped"):
                return real_probe(probe_path)
            holder["swapped"] = True
            result = real_probe(probe_path)
            # Simulate another process winning the race: the stale node is
            # replaced by a live listener between our probe and our unlink.
            os.unlink(probe_path)
            live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            live.bind(probe_path)
            live.listen(1)
            holder["socket"] = live
            return result

        monkeypatch.setattr(
            unix_module,
            "_probe_unix_socket",
            probe_then_swap_live,
        )

        server = RoutingControlUnixServer(make_protocol(), path)
        try:
            with pytest.raises(
                ControlSocketPathError,
                match="changed during startup",
            ):
                await server.start()
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
            assert real_probe(str(path)) is _SocketLiveness.LIVE
        finally:
            holder["socket"].close()
            if os.path.exists(path):
                os.unlink(path)

    run(scenario())


@unix_socket_test
def test_live_socket_swapped_in_during_final_reprobe_is_not_unlinked(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        path = tmp_path / "control.sock"
        original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            original.bind(str(path))
        finally:
            original.close()

        import core.routing_control_unix as unix_module

        real_probe = unix_module._probe_unix_socket
        holder = {}

        def probe_swap_on_reprobe(probe_path):
            holder["calls"] = holder.get("calls", 0) + 1
            if holder["calls"] == 1:
                # First probe (from _prepare_socket_path): untouched stale node.
                return real_probe(probe_path)

            # Second probe (the authoritative re-probe): it inspects the
            # original stale socket and finds it STALE, but another process
            # replaces the path with a live listener before it returns. A
            # throwaway holder consumes the just-freed inode so the live
            # replacement is guaranteed a distinct identity.
            result = real_probe(probe_path)
            os.unlink(probe_path)
            inode_holder = open(tmp_path / "inode_holder", "wb")
            holder["inode_holder"] = inode_holder
            live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            live.bind(probe_path)
            live.listen(1)
            holder["socket"] = live
            return result

        monkeypatch.setattr(
            unix_module,
            "_probe_unix_socket",
            probe_swap_on_reprobe,
        )

        server = RoutingControlUnixServer(make_protocol(), path)
        try:
            with pytest.raises(
                ControlSocketPathError,
                match="changed during startup",
            ):
                await server.start()
            assert holder["calls"] == 2
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
            assert real_probe(str(path)) is _SocketLiveness.LIVE
        finally:
            holder["socket"].close()
            holder["inode_holder"].close()
            if os.path.exists(path):
                os.unlink(path)

    run(scenario())


# --- Point 9 D: permissions established before the listener accepts ---


def _read_process_umask() -> int:
    current = os.umask(0o022)
    os.umask(current)
    return current


@unix_socket_test
def test_configured_mode_is_established_before_asyncio_serves(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        path = tmp_path / "control.sock"
        observed = {}
        real_start_unix_server = asyncio.start_unix_server

        async def spy_start_unix_server(handler, *args, sock=None, **kwargs):
            observed["sockname"] = sock.getsockname()
            observed["mode"] = stat.S_IMODE(os.lstat(path).st_mode)
            return await real_start_unix_server(
                handler,
                *args,
                sock=sock,
                **kwargs,
            )

        monkeypatch.setattr(asyncio, "start_unix_server", spy_start_unix_server)

        previous_umask = os.umask(0o000)
        try:
            server = RoutingControlUnixServer(
                make_protocol(),
                path,
                socket_mode=0o660,
            )
            await server.start()
        finally:
            os.umask(previous_umask)

        try:
            assert observed["sockname"] == str(path)
            assert observed["mode"] == 0o660
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o660
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_bind_umask_is_restored_after_successful_start(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        before = _read_process_umask()
        server = RoutingControlUnixServer(
            make_protocol(),
            path,
            socket_mode=0o600,
        )
        await server.start()
        try:
            assert _read_process_umask() == before
        finally:
            await server.close()

    run(scenario())


@unix_socket_test
def test_bind_umask_is_restored_after_bind_failure(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "control.sock"
        before = _read_process_umask()

        import core.routing_control_unix as unix_module

        real_socket_factory = socket.socket
        created = []

        class BindFailsSocket:
            def __init__(self, *args):
                self._wrapped = real_socket_factory(*args)
                self.closed = False

            def bind(self, _address):
                raise OSError("bind boom")

            def close(self):
                self.closed = True
                self._wrapped.close()

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def factory(*args):
            made = BindFailsSocket(*args)
            created.append(made)
            return made

        monkeypatch.setattr(unix_module.socket, "socket", factory)

        server = RoutingControlUnixServer(
            make_protocol(),
            path,
            socket_mode=0o640,
        )
        with pytest.raises(OSError, match="bind boom"):
            await server.start()

        assert _read_process_umask() == before
        assert not path.exists()
        assert created and created[0].closed is True

    run(scenario())


@unix_socket_test
def test_chmod_failure_does_not_remove_a_replacement_socket(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        path = tmp_path / "control.sock"
        server = RoutingControlUnixServer(make_protocol(), path)
        replacement_holder = {}

        def swap_then_fail(chmod_path, _mode):
            os.unlink(chmod_path)
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(chmod_path))
            replacement_holder["socket"] = replacement
            raise PermissionError("chmod denied")

        monkeypatch.setattr(os, "chmod", swap_then_fail)

        try:
            with pytest.raises(PermissionError, match="chmod denied"):
                await server.start()
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
        finally:
            replacement_holder["socket"].close()
            if os.path.exists(path):
                os.unlink(path)

    run(scenario())
