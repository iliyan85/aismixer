import asyncio
import json
import socket

import pytest

import aismixer
from assembler import AIVDMAssembler
from core.event import IngressEvent
from core.python_data_plane import PythonDataPlaneProcessor
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
    def fail_service_factory(_routing_state, _target_id_by_name):
        raise AssertionError("service must not be constructed")

    assert (
        build_optional_routing_control_server(
            {"control": {"unix": {"enabled": False}}},
            RoutingState(),
            {"udp:a": 0},
            service_factory=fail_service_factory,
        )
        is None
    )


def test_enabled_builder_wires_stack_without_starting_server():
    calls = {}
    routing_state = RoutingState()
    target_id_by_name = {"udp:a": 7}

    class FakeServer:
        def __init__(self, protocol, socket_path, *, max_request_bytes, socket_mode):
            calls["server"] = (protocol, socket_path, max_request_bytes, socket_mode)
            self.start_count = 0

        async def start(self):
            self.start_count += 1

    def service_factory(state, supplied_target_id_by_name):
        calls["service"] = (state, supplied_target_id_by_name)
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
        target_id_by_name,
        service_factory=service_factory,
        protocol_factory=protocol_factory,
        server_factory=FakeServer,
    )

    assert isinstance(server, FakeServer)
    assert server.start_count == 0
    assert calls["service"] == (routing_state, target_id_by_name)
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
        {"udp:a": 0},
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
    supervisor_exc=None,
    observer=None,
):
    observer = observer if observer is not None else {}
    routing_state = RoutingState()

    class FakeForwarder:
        all_target_ids = (0,)
        target_id_by_name = {"udp:a": 0}
        target_ids = ("udp:a",)

        def __init__(self):
            self.close_count = 0

        def close(self):
            self.close_count += 1

    forwarder = FakeForwarder()
    builder_calls = []
    supervisor_called = {
        "value": False,
        "specs": (),
    }

    def fake_builder(config, state, target_id_by_name):
        builder_calls.append((config, state, target_id_by_name))
        return control_server

    async def fake_supervise_named_tasks(task_specs):
        observer["supervisor_called"] = True
        supervisor_called["value"] = True
        supervisor_called["specs"] = tuple(task_specs)
        if supervisor_exc is not None:
            raise supervisor_exc

    monkeypatch.setattr(aismixer, "SEC_INPUTS", [])
    monkeypatch.setattr(aismixer, "UDP_INPUTS", [])
    monkeypatch.setattr(aismixer, "config", {"control": None})
    monkeypatch.setattr(aismixer, "routing_state", routing_state)
    monkeypatch.setattr(aismixer, "forwarder", forwarder)
    monkeypatch.setattr(aismixer, "build_optional_routing_control_server", fake_builder)
    monkeypatch.setattr(
        aismixer,
        "_supervise_named_tasks",
        fake_supervise_named_tasks,
    )

    await aismixer.main()

    return {
        "builder_calls": builder_calls,
        "supervisor_called": supervisor_called,
        "routing_state": routing_state,
        "forwarder": forwarder,
        "forwarder_close_count": forwarder.close_count,
    }


def test_disabled_control_runtime_does_not_start_server(monkeypatch):
    result = asyncio.run(run_aismixer_main(monkeypatch, control_server=None))

    assert result["builder_calls"] == [
        ({"control": None}, result["routing_state"], {"udp:a": 0})
    ]
    assert result["supervisor_called"]["value"] is True
    specs = {
        spec.name: spec
        for spec in result["supervisor_called"]["specs"]
    }
    assert tuple(specs) == (
        "ingress-fan-in",
        "processor-stage",
        "egress-stage",
    )
    processor_factory = specs["processor-stage"].coroutine_factory
    egress_factory = specs["egress-stage"].coroutine_factory
    assert processor_factory.keywords == {
        "routing_state": result["routing_state"],
        "processor": aismixer.data_plane_processor,
        "legacy_target_ids": result["forwarder"].all_target_ids,
    }
    assert egress_factory.args[1] is result["forwarder"]
    assert egress_factory.keywords == {
        "debug": aismixer.DEBUG,
        "timestamp": aismixer.ts,
    }
    assert processor_factory.args[1] is egress_factory.args[0]
    assert egress_factory.args[0].maxsize == 1
    assert result["forwarder_close_count"] == 1


def test_enabled_control_starts_and_closes_server(monkeypatch):
    server = FakeControlServer()

    result = asyncio.run(run_aismixer_main(monkeypatch, control_server=server))

    assert server.start_count == 1
    assert server.close_count == 1
    assert result["forwarder_close_count"] == 1


def test_server_closes_when_runtime_supervisor_raises(monkeypatch):
    server = FakeControlServer()

    with pytest.raises(RuntimeError, match="forward failed"):
        asyncio.run(
            run_aismixer_main(
                monkeypatch,
                control_server=server,
                supervisor_exc=RuntimeError("forward failed"),
            )
        )

    assert server.start_count == 1
    assert server.close_count == 1


def test_server_closes_when_runtime_supervisor_is_cancelled(monkeypatch):
    server = FakeControlServer()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_aismixer_main(
                monkeypatch,
                control_server=server,
                supervisor_exc=asyncio.CancelledError(),
            )
        )

    assert server.start_count == 1
    assert server.close_count == 1


def test_server_start_failure_prevents_runtime_supervisor_and_propagates(monkeypatch):
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
    assert observer.get("supervisor_called") is None


class _MainTestSocket:
    def __init__(self, bind_exc=None):
        self.bind_exc = bind_exc
        self.bind_calls = []
        self.close_count = 0

    def bind(self, address):
        self.bind_calls.append(address)
        if self.bind_exc is not None:
            raise self.bind_exc

    def setblocking(self, _blocking):
        return None

    def close(self):
        self.close_count += 1


class _MainTestSocketFactory:
    def __init__(self, sockets):
        self._sockets = iter(sockets)

    def __call__(self, _listen_ip, *, reuse_address):
        assert reuse_address is True
        return next(self._sockets)


class _MainTestForwarder:
    all_target_ids = (0,)
    target_id_by_name = {"udp:target": 0}
    target_ids = ("udp:target",)

    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


def _configure_main_lifecycle_test(
    monkeypatch,
    *,
    sec_inputs=(),
    udp_inputs=(),
    sockets=(),
    control_server=None,
    supervisor=None,
):
    state = RoutingState()
    output_forwarder = _MainTestForwarder()

    monkeypatch.setattr(aismixer, "SEC_INPUTS", list(sec_inputs))
    monkeypatch.setattr(aismixer, "UDP_INPUTS", list(udp_inputs))
    monkeypatch.setattr(aismixer, "config", {"control": None})
    monkeypatch.setattr(aismixer, "routing_state", state)
    monkeypatch.setattr(aismixer, "forwarder", output_forwarder)
    monkeypatch.setattr(
        aismixer,
        "create_udp_listener_socket",
        _MainTestSocketFactory(sockets),
    )
    monkeypatch.setattr(
        aismixer,
        "build_optional_routing_control_server",
        lambda *_args: control_server,
    )
    if supervisor is not None:
        monkeypatch.setattr(
            aismixer,
            "_supervise_named_tasks",
            supervisor,
        )

    return state, output_forwarder


def test_main_hands_every_essential_role_to_one_runtime_supervisor(monkeypatch):
    async def scenario():
        udp_socket = _MainTestSocket()
        supervision_calls = []

        async def fake_supervisor(task_specs):
            supervision_calls.append(tuple(task_specs))

        state, output_forwarder = _configure_main_lifecycle_test(
            monkeypatch,
            sec_inputs=(
                {
                    "id": "secure_station",
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10111,
                },
            ),
            udp_inputs=(
                {
                    "id": "plain_station",
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10110,
                },
            ),
            sockets=(udp_socket,),
            supervisor=fake_supervisor,
        )

        await aismixer.main()

        assert len(supervision_calls) == 1
        specs = supervision_calls[0]
        assert [spec.name for spec in specs] == [
            "udpsec-ingress:0:secure_station",
            "udp-ingress:0:plain_station",
            "ingress-fan-in",
            "processor-stage",
            "egress-stage",
        ]

        secure_factory = specs[0].coroutine_factory
        udp_factory = specs[1].coroutine_factory
        fan_in_factory = specs[2].coroutine_factory
        processor_factory = specs[3].coroutine_factory
        egress_factory = specs[4].coroutine_factory

        assert secure_factory.func is aismixer.secure_server
        assert secure_factory.args[1:] == ("127.0.0.1", 10111)
        assert secure_factory.keywords["sec_input_id"] == "secure_station"
        assert udp_factory.func is aismixer.handle_socket
        assert udp_factory.args[0] is udp_socket
        assert udp_factory.args[2] == "plain_station"
        assert fan_in_factory.func is aismixer.ingress_fan_in_loop
        assert fan_in_factory.args[0] == (
            secure_factory.args[0],
            udp_factory.args[1],
        )
        assert processor_factory.func is aismixer.processor_stage_loop
        assert processor_factory.keywords == {
            "routing_state": state,
            "processor": aismixer.data_plane_processor,
            "legacy_target_ids": output_forwarder.all_target_ids,
        }
        assert egress_factory.func is aismixer.egress_stage_loop
        assert egress_factory.args[1] is output_forwarder
        assert udp_socket.close_count == 1
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


def test_partial_listener_startup_failure_closes_started_resources(monkeypatch):
    async def scenario():
        control_server = FakeControlServer()
        first_socket = _MainTestSocket()
        bind_failure = OSError("second bind failed")
        second_socket = _MainTestSocket(bind_exc=bind_failure)
        supervisor_called = False

        async def fake_supervisor(_task_specs):
            nonlocal supervisor_called
            supervisor_called = True

        _, output_forwarder = _configure_main_lifecycle_test(
            monkeypatch,
            udp_inputs=(
                {
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10110,
                },
                {
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10111,
                },
            ),
            sockets=(first_socket, second_socket),
            control_server=control_server,
            supervisor=fake_supervisor,
        )

        with pytest.raises(OSError) as excinfo:
            await aismixer.main()

        assert excinfo.value is bind_failure
        assert supervisor_called is False
        assert first_socket.close_count == 1
        assert second_socket.close_count == 1
        assert control_server.start_count == 1
        assert control_server.close_count == 1
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


def test_runtime_failure_closes_each_main_resource_exactly_once(monkeypatch):
    async def scenario():
        failure = RuntimeError("runtime failed")
        control_server = FakeControlServer()
        udp_socket = _MainTestSocket()

        async def failing_supervisor(_task_specs):
            raise failure

        _, output_forwarder = _configure_main_lifecycle_test(
            monkeypatch,
            udp_inputs=(
                {
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10110,
                },
            ),
            sockets=(udp_socket,),
            control_server=control_server,
            supervisor=failing_supervisor,
        )

        with pytest.raises(RuntimeError) as excinfo:
            await aismixer.main()

        assert excinfo.value is failure
        assert udp_socket.close_count == 1
        assert control_server.start_count == 1
        assert control_server.close_count == 1
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


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
    all_target_ids = (0,)
    target_id_by_name = {"udp:target": 0}
    target_ids = ("udp:target",)

    def __init__(self):
        self.targeted_messages = []
        self.targeted_event = asyncio.Event()

    async def send(self, _message):
        raise AssertionError("runtime egress must use numeric target IDs")

    async def send_to_ids(self, target_ids, message):
        self.targeted_messages.append((tuple(target_ids), message))
        self.targeted_event.set()

    async def send_to(self, _target_ids, _message):
        raise AssertionError("runtime targeted egress must use numeric target IDs")


@unix_socket_test
def test_runtime_control_unix_stack_updates_staged_routing(tmp_path):
    async def scenario():
        path = tmp_path / "control.sock"
        routing_state = RoutingState()
        fake_forwarder = IntegrationForwarder()
        config = enabled_config(socket_path=str(path))
        server = build_optional_routing_control_server(
            config,
            routing_state,
            fake_forwarder.target_id_by_name,
        )
        assert isinstance(server, RoutingControlUnixServer)

        processor = PythonDataPlaneProcessor(
            station_id="test_station",
            preserve_ingress_c=True,
            preserve_ingress_gid=True,
            always_tag_single=False,
            assembler=AIVDMAssembler(),
            deduplicator=Deduplicator(),
        )

        await server.start()
        client = RoutingControlUnixClient(path)
        ingress_queue = asyncio.Queue()
        egress_queue = asyncio.Queue()
        runtime_task = asyncio.create_task(
            aismixer._run_runtime_stages(
                ingress_queue,
                egress_queue,
                routing_state=routing_state,
                processor=processor,
                output_forwarder=fake_forwarder,
                legacy_target_ids=fake_forwarder.all_target_ids,
                debug=False,
            )
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

            await ingress_queue.put(make_event(SENTENCE))
            await asyncio.wait_for(fake_forwarder.targeted_event.wait(), timeout=1)
            fake_forwarder.targeted_event.clear()

            disable = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "disable-1",
                    "method": "routing.disable",
                    "params": {"expected_generation": 1},
                }
            )

            await ingress_queue.put(make_event(SECOND_SENTENCE))
            await asyncio.wait_for(fake_forwarder.targeted_event.wait(), timeout=1)
        finally:
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)
            await server.close()

        assert status["result"]["generation"] == 0
        assert replace["result"]["generation"] == 1
        assert routing_state.snapshot().generation == 2
        assert disable["result"]["generation"] == 2
        assert [
            target_ids
            for target_ids, _message in fake_forwarder.targeted_messages
        ] == [(0,), (0,)]
        assert not path.exists()

    asyncio.run(scenario())
