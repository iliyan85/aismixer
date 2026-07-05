import asyncio
import json
import socket

import pytest

import aismixer
from assembler import AIVDMAssembler
from core.event import IngressEvent
from core.routing_control_protocol import ROUTING_CONTROL_PROTOCOL_VERSION
from core.routing_control_unix import RoutingControlUnixServer
from core.routing_control_unix_client import RoutingControlUnixClient
from core.routing_state import RoutingState
from core.runtime_control import (
    DEFAULT_CONTROL_MAX_REQUEST_BYTES,
    DEFAULT_CONTROL_SOCKET_MODE,
    RuntimeControlConfigError,
    RoutingControlUnixSettings,
    build_optional_routing_control_server,
    load_optional_routing_control_unix_settings,
)
from dedup import Deduplicator


SENTENCE = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C"
SECOND_SENTENCE = "!AIVDM,1,1,,B,25Muq?002>G?svP00<:O?vN60<0,0*00"

HAS_UNIX_SOCKETS = (
    hasattr(socket, "AF_UNIX")
    and hasattr(asyncio, "start_unix_server")
    and hasattr(asyncio, "open_unix_connection")
)
unix_socket_test = pytest.mark.skipif(
    not HAS_UNIX_SOCKETS,
    reason="Unix-domain asyncio sockets are not supported on this platform.",
)


def enabled_config(**unix_overrides):
    unix = {
        "enabled": True,
        "socket_path": "/run/aismixer/control.sock",
    }
    unix.update(unix_overrides)
    return {"control": {"unix": unix}}


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"control": None},
        {"control": {}},
        {"control": {"unix": None}},
        {"control": {"unix": {"enabled": False}}},
    ],
)
def test_disabled_control_shapes_return_none(config):
    assert load_optional_routing_control_unix_settings(config) is None


def test_enabled_true_loads_settings():
    settings = load_optional_routing_control_unix_settings(
        enabled_config(
            socket_path="/tmp/control.sock",
            socket_mode="0660",
            max_request_bytes=2048,
        )
    )

    assert settings == RoutingControlUnixSettings(
        socket_path="/tmp/control.sock",
        max_request_bytes=2048,
        socket_mode=0o660,
    )


def test_enabled_is_required_when_unix_mapping_exists():
    with pytest.raises(RuntimeControlConfigError, match="enabled"):
        load_optional_routing_control_unix_settings({"control": {"unix": {}}})


@pytest.mark.parametrize("enabled", [0, 1, "true", object()])
def test_non_bool_enabled_is_rejected(enabled):
    with pytest.raises(RuntimeControlConfigError, match="enabled"):
        load_optional_routing_control_unix_settings(
            {"control": {"unix": {"enabled": enabled}}}
        )


@pytest.mark.parametrize(
    "config",
    [
        {"control": []},
        {"control": {"unix": []}},
    ],
)
def test_control_and_unix_must_be_mappings(config):
    with pytest.raises(RuntimeControlConfigError, match="mapping"):
        load_optional_routing_control_unix_settings(config)


def test_unknown_control_fields_are_rejected_deterministically():
    with pytest.raises(RuntimeControlConfigError) as exc_info:
        load_optional_routing_control_unix_settings(
            {"control": {"z": True, "a": True, "unix": None}}
        )

    assert str(exc_info.value).endswith("a, z.")


def test_unknown_unix_fields_are_rejected_deterministically():
    with pytest.raises(RuntimeControlConfigError) as exc_info:
        load_optional_routing_control_unix_settings(
            {"control": {"unix": {"enabled": False, "z": True, "a": True}}}
        )

    assert str(exc_info.value).endswith("a, z.")


def test_enabled_true_requires_socket_path():
    with pytest.raises(RuntimeControlConfigError, match="socket_path"):
        load_optional_routing_control_unix_settings({"control": {"unix": {"enabled": True}}})


@pytest.mark.parametrize("socket_path", ["", b"/run/aismixer/control.sock"])
def test_invalid_socket_path_is_rejected(socket_path):
    with pytest.raises(RuntimeControlConfigError, match="socket_path"):
        load_optional_routing_control_unix_settings(
            enabled_config(socket_path=socket_path)
        )


def test_default_max_request_bytes_and_socket_mode():
    settings = load_optional_routing_control_unix_settings(enabled_config())

    assert settings.max_request_bytes == DEFAULT_CONTROL_MAX_REQUEST_BYTES
    assert settings.socket_mode == DEFAULT_CONTROL_SOCKET_MODE


def test_custom_max_request_bytes_is_accepted():
    settings = load_optional_routing_control_unix_settings(
        enabled_config(max_request_bytes=4096)
    )

    assert settings.max_request_bytes == 4096


@pytest.mark.parametrize("max_request_bytes", [0, -1, True, "1048576"])
def test_invalid_max_request_bytes_is_rejected(max_request_bytes):
    with pytest.raises(RuntimeControlConfigError, match="max_request_bytes"):
        load_optional_routing_control_unix_settings(
            enabled_config(max_request_bytes=max_request_bytes)
        )


@pytest.mark.parametrize(
    ("socket_mode", "expected"),
    [
        (0o600, 0o600),
        ("660", 0o660),
        ("0660", 0o660),
    ],
)
def test_socket_mode_valid_forms_are_accepted(socket_mode, expected):
    settings = load_optional_routing_control_unix_settings(
        enabled_config(socket_mode=socket_mode)
    )

    assert settings.socket_mode == expected


@pytest.mark.parametrize(
    "socket_mode",
    [True, -1, 0o1000, "668", "0888", "6600", "u=rw,g=rw", "abc"],
)
def test_invalid_socket_mode_is_rejected(socket_mode):
    with pytest.raises(RuntimeControlConfigError, match="socket_mode"):
        load_optional_routing_control_unix_settings(
            enabled_config(socket_mode=socket_mode)
        )


def test_disabled_builder_returns_none_without_constructing_stack():
    def fail_service_factory(_routing_state, _available_target_ids):
        raise AssertionError("service must not be constructed")

    assert (
        build_optional_routing_control_server(
            {"control": {"unix": {"enabled": False}}},
            RoutingState(),
            ("udp:a",),
            service_factory=fail_service_factory,
        )
        is None
    )


def test_enabled_builder_wires_stack_without_starting_server():
    calls = {}
    routing_state = RoutingState()
    target_ids = ("udp:a",)

    class FakeServer:
        def __init__(self, protocol, socket_path, *, max_request_bytes, socket_mode):
            calls["server"] = (protocol, socket_path, max_request_bytes, socket_mode)
            self.start_count = 0

        async def start(self):
            self.start_count += 1

    def service_factory(state, available_target_ids):
        calls["service"] = (state, available_target_ids)
        return "service"

    def protocol_factory(service):
        calls["protocol"] = service
        return "protocol"

    server = build_optional_routing_control_server(
        enabled_config(
            socket_path="/tmp/control.sock",
            max_request_bytes=1234,
            socket_mode="0600",
        ),
        routing_state,
        target_ids,
        service_factory=service_factory,
        protocol_factory=protocol_factory,
        server_factory=FakeServer,
    )

    assert isinstance(server, FakeServer)
    assert server.start_count == 0
    assert calls["service"] == (routing_state, target_ids)
    assert calls["protocol"] == "service"
    assert calls["server"] == ("protocol", "/tmp/control.sock", 1234, 0o600)


def test_builder_stack_updates_supplied_routing_state_without_real_socket():
    class CapturingServer:
        def __init__(self, protocol, socket_path, *, max_request_bytes, socket_mode):
            self.protocol = protocol

    routing_state = RoutingState()
    server = build_optional_routing_control_server(
        enabled_config(socket_path="/tmp/control.sock"),
        routing_state,
        ("udp:a",),
        server_factory=CapturingServer,
    )

    response = json.loads(
        server.protocol.handle_json(
            json.dumps(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "replace-1",
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
        )
    )

    assert response["ok"] is True
    assert routing_state.snapshot().generation == 1


class FakeControlServer:
    def __init__(self, start_exc=None):
        self.start_exc = start_exc
        self.start_count = 0
        self.close_count = 0

    async def start(self):
        self.start_count += 1
        if self.start_exc is not None:
            raise self.start_exc

    async def close(self):
        self.close_count += 1


async def run_aismixer_main(
    monkeypatch,
    *,
    control_server=None,
    forward_exc=None,
    observer=None,
):
    observer = observer if observer is not None else {}
    routing_state = RoutingState()
    forwarder = type("FakeForwarder", (), {"target_ids": ("udp:a",)})()
    builder_calls = []
    mixer_cancelled = {"value": False}
    forward_called = {"value": False, "routing_state": None}
    mixer_started = asyncio.Event()

    def fake_builder(config, state, available_target_ids):
        builder_calls.append((config, state, available_target_ids))
        return control_server

    async def fake_mixer_loop(_input_queues, _output_queue):
        mixer_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            mixer_cancelled["value"] = True
            raise

    async def fake_forward_loop(_queue, routing_state=None):
        observer["forward_called"] = True
        forward_called["value"] = True
        forward_called["routing_state"] = routing_state
        await mixer_started.wait()
        if forward_exc is not None:
            raise forward_exc

    monkeypatch.setattr(aismixer, "SEC_INPUTS", [])
    monkeypatch.setattr(aismixer, "UDP_INPUTS", [])
    monkeypatch.setattr(aismixer, "config", {"control": None})
    monkeypatch.setattr(aismixer, "routing_state", routing_state)
    monkeypatch.setattr(aismixer, "forwarder", forwarder)
    monkeypatch.setattr(aismixer, "build_optional_routing_control_server", fake_builder)
    monkeypatch.setattr(aismixer, "mixer_loop", fake_mixer_loop)
    monkeypatch.setattr(aismixer, "forward_loop", fake_forward_loop)

    await aismixer.main()

    return {
        "builder_calls": builder_calls,
        "mixer_cancelled": mixer_cancelled["value"],
        "forward_called": forward_called,
        "routing_state": routing_state,
    }


def test_disabled_control_runtime_does_not_start_server(monkeypatch):
    result = asyncio.run(run_aismixer_main(monkeypatch, control_server=None))

    assert result["builder_calls"] == [
        ({"control": None}, result["routing_state"], ("udp:a",))
    ]
    assert result["forward_called"]["value"] is True
    assert result["forward_called"]["routing_state"] is result["routing_state"]
    assert result["mixer_cancelled"] is True


def test_enabled_control_starts_and_closes_server(monkeypatch):
    server = FakeControlServer()

    result = asyncio.run(run_aismixer_main(monkeypatch, control_server=server))

    assert server.start_count == 1
    assert server.close_count == 1
    assert result["mixer_cancelled"] is True


def test_server_closes_when_forward_loop_raises(monkeypatch):
    server = FakeControlServer()

    with pytest.raises(RuntimeError, match="forward failed"):
        asyncio.run(
            run_aismixer_main(
                monkeypatch,
                control_server=server,
                forward_exc=RuntimeError("forward failed"),
            )
        )

    assert server.start_count == 1
    assert server.close_count == 1


def test_server_closes_when_forward_loop_is_cancelled(monkeypatch):
    server = FakeControlServer()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_aismixer_main(
                monkeypatch,
                control_server=server,
                forward_exc=asyncio.CancelledError(),
            )
        )

    assert server.start_count == 1
    assert server.close_count == 1


def test_server_start_failure_prevents_forward_loop_and_propagates(monkeypatch):
    server = FakeControlServer(start_exc=PermissionError("bind denied"))
    observer = {}

    with pytest.raises(PermissionError, match="bind denied"):
        asyncio.run(
            run_aismixer_main(
                monkeypatch,
                control_server=server,
                observer=observer,
            )
        )

    assert server.start_count == 1
    assert server.close_count == 0
    assert observer.get("forward_called") is None


def test_main_does_not_construct_a_second_routing_state(monkeypatch):
    def fail_routing_state(*_args, **_kwargs):
        raise AssertionError("RoutingState must not be constructed in main")

    monkeypatch.setattr(aismixer, "RoutingState", fail_routing_state)

    asyncio.run(run_aismixer_main(monkeypatch, control_server=None))


def make_event(raw_line, source_id="udp:source"):
    return IngressEvent(
        kind="udp",
        source_id=source_id,
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key=raw_line,
        raw_line=raw_line,
    )


class IntegrationForwarder:
    target_ids = ("udp:target",)

    def __init__(self):
        self.messages = []
        self.targeted_messages = []
        self.broadcast_event = asyncio.Event()
        self.targeted_event = asyncio.Event()

    async def send(self, message):
        self.messages.append(message)
        self.broadcast_event.set()

    async def send_to(self, target_ids, message):
        self.targeted_messages.append((tuple(target_ids), message))
        self.targeted_event.set()


@unix_socket_test
def test_runtime_control_unix_stack_updates_forward_loop_routing(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "control.sock"
        routing_state = RoutingState()
        fake_forwarder = IntegrationForwarder()
        config = enabled_config(socket_path=str(path))
        server = build_optional_routing_control_server(
            config,
            routing_state,
            fake_forwarder.target_ids,
        )
        assert isinstance(server, RoutingControlUnixServer)

        monkeypatch.setattr(aismixer, "forwarder", fake_forwarder)
        monkeypatch.setattr(aismixer, "deduplicator", Deduplicator())
        monkeypatch.setattr(aismixer, "assembler", AIVDMAssembler())
        monkeypatch.setattr(aismixer, "STATION_ID", "test_station")
        monkeypatch.setattr(aismixer, "DEBUG", False)
        monkeypatch.setattr(aismixer, "C_PRESERVE_INGRESS_C", True)
        monkeypatch.setattr(aismixer, "G_PRESERVE_INGRESS_GID", True)
        monkeypatch.setattr(aismixer, "G_ALWAYS_TAG_SINGLE", False)

        await server.start()
        client = RoutingControlUnixClient(path)
        queue = asyncio.Queue()
        forward_task = asyncio.create_task(
            aismixer.forward_loop(queue, routing_state=routing_state)
        )
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
                    "params": {
                        "routing": {
                            "zones": {"source": {"include": ["udp:source"]}},
                            "routes": [
                                {
                                    "name": "source_to_target",
                                    "from_zone": "source",
                                    "to": ["udp:target"],
                                }
                            ],
                        }
                    },
                }
            )

            await queue.put(make_event(SENTENCE))
            await asyncio.wait_for(fake_forwarder.targeted_event.wait(), timeout=1)

            disable = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "disable-1",
                    "method": "routing.disable",
                    "params": {"expected_generation": 1},
                }
            )

            await queue.put(make_event(SECOND_SENTENCE))
            await asyncio.wait_for(fake_forwarder.broadcast_event.wait(), timeout=1)
        finally:
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
            await server.close()

        assert status["result"]["generation"] == 0
        assert replace["result"]["generation"] == 1
        assert routing_state.snapshot().generation == 2
        assert disable["result"]["generation"] == 2
        assert fake_forwarder.targeted_messages[0][0] == ("udp:target",)
        assert fake_forwarder.messages
        assert not path.exists()

    asyncio.run(scenario())
