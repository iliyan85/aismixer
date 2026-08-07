import asyncio
import json
import socket

import pytest

import aismixer
from assembler import AIVDMAssembler
from core.data_plane import DeduplicationMode
from core.event import IngressEvent
from core.metrics import EgressMetricsSnapshot, QueueMetricsSnapshot
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
THIRD_SENTENCE = "!AIVDM,1,1,,A,35Muq?002>G?svP00<:O?vN60<0,0*00"

HAS_UNIX_SOCKETS = (
    hasattr(socket, "AF_UNIX")
    and hasattr(asyncio, "start_unix_server")
    and hasattr(asyncio, "open_unix_connection")
)
unix_socket_test = pytest.mark.skipif(
    not HAS_UNIX_SOCKETS,
    reason="Unix-domain asyncio sockets are not supported on this platform.",
)


def empty_queue_metrics(name, capacity):
    return QueueMetricsSnapshot(
        name=name,
        capacity=capacity,
        depth=0,
        peak_depth=0,
        enqueued=0,
        dequeued=0,
        put_waits=0,
        current_put_waiters=0,
    )


def empty_egress_metrics():
    return EgressMetricsSnapshot(
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


def test_enabled_builder_requires_statistics_before_constructing_stack():
    def fail_service_factory(_routing_state, _target_id_by_name):
        raise AssertionError("service must not be constructed")

    with pytest.raises(TypeError, match="statistics_provider"):
        build_optional_routing_control_server(
            enabled_config(),
            RoutingState(),
            {"udp:a": 0},
            service_factory=fail_service_factory,
        )


def test_enabled_builder_wires_stack_without_starting_server():
    calls = {}
    routing_state = RoutingState()
    target_id_by_name = {"udp:a": 7}
    statistics = object()

    class FakeServer:
        def __init__(self, protocol, socket_path, *, max_request_bytes, socket_mode):
            calls["server"] = (protocol, socket_path, max_request_bytes, socket_mode)
            self.start_count = 0

        async def start(self):
            self.start_count += 1

    def service_factory(state, supplied_target_id_by_name):
        calls["service"] = (state, supplied_target_id_by_name)
        return "service"

    def protocol_factory(service, supplied_statistics):
        calls["protocol"] = (service, supplied_statistics)
        return "protocol"

    server = build_optional_routing_control_server(
        enabled_config(
            socket_path="/tmp/control.sock",
            max_request_bytes=1234,
            socket_mode="0600",
        ),
        routing_state,
        target_id_by_name,
        statistics,
        service_factory=service_factory,
        protocol_factory=protocol_factory,
        server_factory=FakeServer,
    )

    assert isinstance(server, FakeServer)
    assert server.start_count == 0
    assert calls["service"] == (routing_state, target_id_by_name)
    assert calls["protocol"] == ("service", statistics)
    assert calls["server"] == ("protocol", "/tmp/control.sock", 1234, 0o600)


def test_builder_stack_updates_supplied_routing_state_without_real_socket():
    class CapturingServer:
        def __init__(self, protocol, socket_path, *, max_request_bytes, socket_mode):
            self.protocol = protocol

    routing_state = RoutingState()
    statistics = object()
    server = build_optional_routing_control_server(
        enabled_config(socket_path="/tmp/control.sock"),
        routing_state,
        {"udp:a": 0},
        statistics,
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
    processor = object()
    processor_factory_calls = []
    builder_calls = []
    supervisor_called = {
        "value": False,
        "specs": (),
    }

    def fake_builder(config, state, target_id_by_name, statistics):
        builder_calls.append((config, state, target_id_by_name, statistics))
        return control_server

    def fake_create_data_plane_processor():
        processor_factory_calls.append(None)
        return processor

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
    monkeypatch.setattr(
        aismixer,
        "create_data_plane_processor",
        fake_create_data_plane_processor,
    )
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
        "processor": processor,
        "processor_factory_calls": processor_factory_calls,
        "statistics": builder_calls[0][3],
    }


def test_disabled_control_runtime_does_not_start_server(monkeypatch):
    result = asyncio.run(run_aismixer_main(monkeypatch, control_server=None))

    assert result["builder_calls"] == [
        (
            {"control": None},
            result["routing_state"],
            {"udp:a": 0},
            result["statistics"],
        )
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
    fan_in_factory = specs["ingress-fan-in"].coroutine_factory
    processor_factory = specs["processor-stage"].coroutine_factory
    egress_factory = specs["egress-stage"].coroutine_factory
    processing_queue = fan_in_factory.args[1]
    assert fan_in_factory.args[0] == ()
    assert fan_in_factory.keywords == {
        "routing_state": result["routing_state"],
        "legacy_target_ids": result["forwarder"].all_target_ids,
    }
    assert isinstance(processing_queue, aismixer._BoundedProcessingQueue)
    assert (
        processing_queue.maxsize
        == aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE
    )
    assert processing_queue.metrics_snapshot() == empty_queue_metrics(
        "processing",
        aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE,
    )
    assert processor_factory.args[0] is processing_queue
    assert processor_factory.keywords == {
        "processor": result["processor"],
    }
    assert result["processor_factory_calls"] == [None]
    assert egress_factory.args[1] is result["forwarder"]
    egress_metrics = egress_factory.keywords["metrics"]
    assert isinstance(egress_metrics, aismixer._EgressMetrics)
    assert egress_metrics.metrics_snapshot() == empty_egress_metrics()
    assert egress_factory.keywords == {
        "debug": aismixer.DEBUG,
        "timestamp": aismixer.ts,
        "metrics": egress_metrics,
    }
    assert processor_factory.args[1] is egress_factory.args[0]
    egress_queue = egress_factory.args[0]
    assert isinstance(egress_queue, aismixer._ObservedQueue)
    assert egress_queue.metrics_snapshot() == empty_queue_metrics(
        "egress",
        1,
    )
    statistics = result["statistics"]
    assert statistics.ingress_queues == ()
    assert statistics.processing_queue is processing_queue
    assert statistics.processor is result["processor"]
    assert statistics.egress_queue is egress_queue
    assert statistics.egress_operations is egress_metrics
    assert statistics.input_traffic == ()
    assert statistics.output_traffic is result["forwarder"]
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
    processor = object()

    monkeypatch.setattr(aismixer, "SEC_INPUTS", list(sec_inputs))
    monkeypatch.setattr(aismixer, "UDP_INPUTS", list(udp_inputs))
    monkeypatch.setattr(aismixer, "config", {"control": None})
    monkeypatch.setattr(aismixer, "routing_state", state)
    monkeypatch.setattr(aismixer, "forwarder", output_forwarder)
    monkeypatch.setattr(
        aismixer,
        "create_data_plane_processor",
        lambda: processor,
    )
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

    return state, output_forwarder, processor


def test_main_hands_every_essential_role_to_one_runtime_supervisor(monkeypatch):
    async def scenario():
        first_udp_socket = _MainTestSocket()
        second_udp_socket = _MainTestSocket()
        supervision_calls = []
        builder_calls = []

        async def fake_supervisor(task_specs):
            supervision_calls.append(tuple(task_specs))

        state, output_forwarder, processor = _configure_main_lifecycle_test(
            monkeypatch,
            sec_inputs=(
                {
                    "id": "secure_one",
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10111,
                },
                {
                    "id": "secure_two",
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10112,
                },
            ),
            udp_inputs=(
                {
                    "id": "plain_one",
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10110,
                },
                {
                    "id": "plain_two",
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10113,
                },
            ),
            sockets=(first_udp_socket, second_udp_socket),
            supervisor=fake_supervisor,
        )

        def capture_builder(config, supplied_state, target_ids, statistics):
            builder_calls.append(
                (config, supplied_state, target_ids, statistics)
            )
            return None

        monkeypatch.setattr(
            aismixer,
            "build_optional_routing_control_server",
            capture_builder,
        )

        await aismixer.main()

        assert len(supervision_calls) == 1
        specs = supervision_calls[0]
        assert [spec.name for spec in specs] == [
            "udpsec-ingress:0:secure_one",
            "udpsec-ingress:1:secure_two",
            "udp-ingress:0:plain_one",
            "udp-ingress:1:plain_two",
            "ingress-fan-in",
            "processor-stage",
            "egress-stage",
        ]

        secure_factories = tuple(
            spec.coroutine_factory for spec in specs[:2]
        )
        udp_factories = tuple(
            spec.coroutine_factory for spec in specs[2:4]
        )
        fan_in_factory = specs[4].coroutine_factory
        processor_factory = specs[5].coroutine_factory
        egress_factory = specs[6].coroutine_factory

        assert all(
            factory.func is aismixer.secure_server
            for factory in secure_factories
        )
        assert [factory.args[1:] for factory in secure_factories] == [
            ("127.0.0.1", 10111),
            ("127.0.0.1", 10112),
        ]
        assert [
            factory.keywords["sec_input_id"]
            for factory in secure_factories
        ] == ["secure_one", "secure_two"]
        assert all(
            factory.func is aismixer.handle_socket
            for factory in udp_factories
        )
        assert [factory.args[0] for factory in udp_factories] == [
            first_udp_socket,
            second_udp_socket,
        ]
        assert [factory.args[2] for factory in udp_factories] == [
            "plain_one",
            "plain_two",
        ]

        input_queues = tuple(
            factory.args[0] for factory in secure_factories
        ) + tuple(factory.args[1] for factory in udp_factories)
        input_traffic = tuple(
            factory.keywords["input_traffic"]
            for factory in secure_factories + udp_factories
        )
        assert len({id(queue) for queue in input_queues}) == 4
        assert len({id(owner) for owner in input_traffic}) == 4
        assert all(
            isinstance(queue, aismixer._ObservedQueue)
            and queue.maxsize == aismixer.DEFAULT_INGRESS_QUEUE_MAXSIZE
            for queue in input_queues
        )
        assert tuple(
            queue.metrics_snapshot() for queue in input_queues
        ) == tuple(
            empty_queue_metrics(
                spec.name,
                aismixer.DEFAULT_INGRESS_QUEUE_MAXSIZE,
            )
            for spec in specs[:4]
        )
        traffic_snapshots = tuple(
            owner.input_traffic_snapshot() for owner in input_traffic
        )
        assert tuple(snapshot.name for snapshot in traffic_snapshots) == tuple(
            spec.name for spec in specs[:4]
        )
        assert tuple(snapshot.kind for snapshot in traffic_snapshots) == (
            "udpsec",
            "udpsec",
            "udp",
            "udp",
        )
        assert all(
            snapshot.transport_packets == 0
            and snapshot.transport_bytes == 0
            and snapshot.accepted_frames == 0
            and snapshot.payload_bytes == 0
            for snapshot in traffic_snapshots
        )
        assert fan_in_factory.func is aismixer.ingress_fan_in_loop
        assert fan_in_factory.args[0] == input_queues
        assert fan_in_factory.keywords == {
            "routing_state": state,
            "legacy_target_ids": output_forwarder.all_target_ids,
        }
        processing_queue = fan_in_factory.args[1]
        assert isinstance(
            processing_queue,
            aismixer._BoundedProcessingQueue,
        )
        assert (
            processing_queue.maxsize
            == aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE
        )
        assert processing_queue.metrics_snapshot() == empty_queue_metrics(
            "processing",
            aismixer.DEFAULT_PROCESSING_QUEUE_MAXSIZE,
        )
        assert processor_factory.func is aismixer.processor_stage_loop
        assert processor_factory.args[0] is processing_queue
        assert processor_factory.keywords == {
            "processor": processor,
        }
        assert egress_factory.func is aismixer.egress_stage_loop
        assert egress_factory.args[1] is output_forwarder
        egress_queue = egress_factory.args[0]
        assert processor_factory.args[1] is egress_queue
        assert isinstance(egress_queue, aismixer._ObservedQueue)
        assert egress_queue.metrics_snapshot() == empty_queue_metrics(
            "egress",
            1,
        )
        egress_metrics = egress_factory.keywords["metrics"]
        assert isinstance(egress_metrics, aismixer._EgressMetrics)
        assert egress_metrics.metrics_snapshot() == empty_egress_metrics()
        assert egress_factory.keywords == {
            "debug": aismixer.DEBUG,
            "timestamp": aismixer.ts,
            "metrics": egress_metrics,
        }
        assert len(builder_calls) == 1
        assert builder_calls[0][:3] == (
            {"control": None},
            state,
            output_forwarder.target_id_by_name,
        )
        statistics = builder_calls[0][3]
        assert statistics.ingress_queues == input_queues
        assert statistics.processing_queue is processing_queue
        assert statistics.processor is processor
        assert statistics.egress_queue is egress_queue
        assert statistics.egress_operations is egress_metrics
        assert statistics.input_traffic == input_traffic
        assert statistics.input_traffic_snapshot() == traffic_snapshots
        assert statistics.output_traffic is output_forwarder
        assert first_udp_socket.close_count == 1
        assert second_udp_socket.close_count == 1
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


def test_main_accepts_explicit_capacities_with_isolated_input_queues(
    monkeypatch,
):
    async def scenario():
        udp_socket = _MainTestSocket()
        supervision_calls = []

        async def fake_supervisor(task_specs):
            supervision_calls.append(tuple(task_specs))

        _configure_main_lifecycle_test(
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

        await aismixer.main(
            ingress_queue_maxsize=1,
            processing_queue_maxsize=2,
        )

        specs = supervision_calls[0]
        secure_queue = specs[0].coroutine_factory.args[0]
        udp_queue = specs[1].coroutine_factory.args[1]
        fan_in_factory = specs[2].coroutine_factory
        processor_factory = specs[3].coroutine_factory
        egress_factory = specs[4].coroutine_factory

        assert secure_queue is not udp_queue
        assert isinstance(secure_queue, aismixer._ObservedQueue)
        assert isinstance(udp_queue, aismixer._ObservedQueue)
        assert secure_queue.metrics_snapshot() == empty_queue_metrics(
            "udpsec-ingress:0:secure_station",
            1,
        )
        assert udp_queue.metrics_snapshot() == empty_queue_metrics(
            "udp-ingress:0:plain_station",
            1,
        )
        egress_queue = egress_factory.args[0]
        assert isinstance(egress_queue, aismixer._ObservedQueue)
        assert egress_queue.metrics_snapshot() == empty_queue_metrics(
            "egress",
            1,
        )
        egress_metrics = egress_factory.keywords["metrics"]
        assert isinstance(egress_metrics, aismixer._EgressMetrics)
        assert egress_metrics.metrics_snapshot() == empty_egress_metrics()
        secure_queue.put_nowait(object())
        blocked_secure_put = asyncio.create_task(
            secure_queue.put(object())
        )
        await asyncio.sleep(0)
        assert secure_queue.full()
        assert not blocked_secure_put.done()
        assert udp_queue.empty()
        udp_queue.put_nowait(object())
        assert udp_queue.full()

        # Private capacity is isolated; this makes no admission-fairness claim.
        secure_queue.get_nowait()
        await blocked_secure_put
        assert secure_queue.full()

        processing_queue = fan_in_factory.args[1]
        assert processing_queue.metrics_snapshot() == empty_queue_metrics(
            "processing",
            2,
        )
        assert processor_factory.args[0] is processing_queue

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

        _, output_forwarder, _ = _configure_main_lifecycle_test(
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
        assert control_server.start_count == 0
        assert control_server.close_count == 0
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


def test_control_builder_failure_after_listener_setup_closes_resources(
    monkeypatch,
):
    async def scenario():
        udp_socket = _MainTestSocket()
        failure = RuntimeError("control build failed")
        supervisor_called = False

        async def fake_supervisor(_task_specs):
            nonlocal supervisor_called
            supervisor_called = True

        _, output_forwarder, _ = _configure_main_lifecycle_test(
            monkeypatch,
            udp_inputs=(
                {
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10110,
                },
            ),
            sockets=(udp_socket,),
            supervisor=fake_supervisor,
        )

        def failing_builder(*_args):
            raise failure

        monkeypatch.setattr(
            aismixer,
            "build_optional_routing_control_server",
            failing_builder,
        )

        with pytest.raises(RuntimeError) as excinfo:
            await aismixer.main()

        assert excinfo.value is failure
        assert supervisor_called is False
        assert udp_socket.close_count == 1
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


def test_control_start_failure_after_listener_setup_closes_resources(
    monkeypatch,
):
    async def scenario():
        control_server = FakeControlServer(
            start_exc=PermissionError("bind denied")
        )
        udp_socket = _MainTestSocket()
        supervisor_called = False

        async def fake_supervisor(_task_specs):
            nonlocal supervisor_called
            supervisor_called = True

        _, output_forwarder, _ = _configure_main_lifecycle_test(
            monkeypatch,
            udp_inputs=(
                {
                    "listen_ip": "127.0.0.1",
                    "listen_port": 10110,
                },
            ),
            sockets=(udp_socket,),
            control_server=control_server,
            supervisor=fake_supervisor,
        )

        with pytest.raises(PermissionError, match="bind denied"):
            await aismixer.main()

        assert supervisor_called is False
        assert udp_socket.close_count == 1
        assert control_server.start_count == 1
        assert control_server.close_count == 0
        assert output_forwarder.close_count == 1

    asyncio.run(scenario())


def test_runtime_failure_closes_each_main_resource_exactly_once(monkeypatch):
    async def scenario():
        failure = RuntimeError("runtime failed")
        control_server = FakeControlServer()
        udp_socket = _MainTestSocket()

        async def failing_supervisor(_task_specs):
            raise failure

        _, output_forwarder, _ = _configure_main_lifecycle_test(
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
    all_target_ids = (0, 1)
    target_id_by_name = {"udp:target": 1}
    target_ids = ("udp:legacy", "udp:target")

    def __init__(self):
        self.targeted_messages = []
        self.targeted_event = asyncio.Event()
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()

    async def send(self, _message):
        raise AssertionError("runtime egress must use numeric target IDs")

    async def send_to_ids(self, target_ids, message):
        self.targeted_messages.append((tuple(target_ids), message))
        self.targeted_event.set()
        if len(self.targeted_messages) == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()

    async def send_to(self, _target_ids, _message):
        raise AssertionError("runtime targeted egress must use numeric target IDs")


@unix_socket_test
def test_runtime_control_unix_stack_updates_staged_routing(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        path = tmp_path / "control.sock"
        routing_state = RoutingState()
        fake_forwarder = IntegrationForwarder()
        bound_work_items = []
        work_item_bound = asyncio.Event()
        original_bind = aismixer._bind_processing_work_item

        def observe_bind(*args, **kwargs):
            work_item = original_bind(*args, **kwargs)
            if work_item is not None:
                bound_work_items.append(work_item)
                work_item_bound.set()
            return work_item

        monkeypatch.setattr(
            aismixer,
            "_bind_processing_work_item",
            observe_bind,
        )

        async def wait_for_bound_count(expected_count):
            while len(bound_work_items) < expected_count:
                work_item_bound.clear()
                if len(bound_work_items) >= expected_count:
                    break
                await asyncio.wait_for(work_item_bound.wait(), timeout=1)

        async def wait_for_send_count(expected_count):
            while len(fake_forwarder.targeted_messages) < expected_count:
                fake_forwarder.targeted_event.clear()
                if len(fake_forwarder.targeted_messages) >= expected_count:
                    break
                await asyncio.wait_for(
                    fake_forwarder.targeted_event.wait(),
                    timeout=1,
                )

        config = enabled_config(socket_path=str(path))
        server = build_optional_routing_control_server(
            config,
            routing_state,
            fake_forwarder.target_id_by_name,
            object(),
        )
        assert isinstance(server, RoutingControlUnixServer)

        processor_delegate = PythonDataPlaneProcessor(
            station_id="test_station",
            preserve_ingress_c=True,
            preserve_ingress_gid=True,
            always_tag_single=False,
            assembler=AIVDMAssembler(),
            deduplicator=Deduplicator(),
        )

        class ResetRecordingProcessor:
            def __init__(self, delegate):
                self._delegate = delegate
                self.reset_calls = 0

            def process(self, frame, snapshot):
                return self._delegate.process(frame, snapshot)

            def reset(self):
                self.reset_calls += 1
                return self._delegate.reset()

        processor = ResetRecordingProcessor(processor_delegate)

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
            await asyncio.wait_for(
                fake_forwarder.first_send_started.wait(),
                timeout=1,
            )

            await ingress_queue.put(make_event(SECOND_SENTENCE))
            await wait_for_bound_count(2)

            disable = await client.request(
                {
                    "version": ROUTING_CONTROL_PROTOCOL_VERSION,
                    "request_id": "disable-1",
                    "method": "routing.disable",
                    "params": {"expected_generation": 1},
                }
            )

            await ingress_queue.put(make_event(THIRD_SENTENCE))
            await wait_for_bound_count(3)

            assert len(fake_forwarder.targeted_messages) == 1
            fake_forwarder.release_first_send.set()
            await wait_for_send_count(3)
        finally:
            fake_forwarder.release_first_send.set()
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)
            await server.close()

        assert status["result"]["generation"] == 0
        assert replace["result"]["generation"] == 1
        assert routing_state.snapshot().generation == 2
        assert disable["result"]["generation"] == 2
        assert [
            (
                work_item.snapshot.routing_generation,
                work_item.snapshot.deduplication_mode,
                work_item.snapshot.target_ids,
            )
            for work_item in bound_work_items
        ] == [
            (1, DeduplicationMode.PER_TARGET, (1,)),
            (1, DeduplicationMode.PER_TARGET, (1,)),
            (2, DeduplicationMode.GLOBAL, (0, 1)),
        ]
        assert [
            target_ids
            for target_ids, _message in fake_forwarder.targeted_messages
        ] == [(1,), (1,), (0, 1)]
        assert processor.reset_calls == 0
        assert not path.exists()

    asyncio.run(scenario())
