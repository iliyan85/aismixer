import builtins
import importlib.util
import itertools
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"


def load_proxy_module():
    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "nmea_sproxy_serial_tests", NMEA_SPROXY_DIR / "nmea_sproxy.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))


def load_input_adapters():
    spec = importlib.util.spec_from_file_location(
        "nmea_sproxy_input_adapters_tests",
        NMEA_SPROXY_DIR / "input_adapters.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serial_proxy_config(port="COM4", **overrides):
    input_config = {"type": "serial", "port": port}
    input_config.update(overrides)
    return {
        "input": input_config,
        "remote_host": "192.0.2.10",
        "remote_port": 19999,
        "station_id": "boat_001",
    }


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_absent_input_preserves_top_level_udp_configuration():
    proxy = load_proxy_module()
    config = {"listen_ip": "::", "listen_port": 50000}

    input_config = proxy.validate_local_input_config(config)

    assert input_config == {
        "type": "udp",
        "listen_ip": "::",
        "listen_port": 50000,
    }
    assert "input" not in config


def test_explicit_udp_input_normalizes_listener_fields():
    proxy = load_proxy_module()
    config = {
        "input": {
            "type": "udp",
            "listen_ip": "2001:db8:42::10",
            "listen_port": 50123,
        },
        "listen_ip": "127.0.0.1",
        "listen_port": 50000,
    }

    input_config = proxy.validate_local_input_config(config)

    assert input_config == {
        "type": "udp",
        "listen_ip": "2001:db8:42::10",
        "listen_port": 50123,
    }
    assert config["input"] == input_config


def test_udp_allow_from_normalizes_in_both_supported_forms():
    proxy = load_proxy_module()
    allow_from = ["2001:db8:42::15", "2001:db8:42::/64"]

    top_level = proxy.validate_local_input_config({
        "listen_ip": "::",
        "listen_port": 50000,
        "allow_from": allow_from,
    })
    explicit = proxy.validate_local_input_config({
        "input": {
            "type": "udp",
            "listen_ip": "::",
            "listen_port": 50000,
            "allow_from": allow_from,
        }
    })

    assert top_level == explicit == {
        "type": "udp",
        "listen_ip": "::",
        "listen_port": 50000,
        "allow_from": allow_from,
    }
    policy = proxy.compile_local_ingress_policy(explicit)
    assert policy.allows("2001:db8:42::15")
    assert not policy.allows("2001:db8:43::15")


def test_udp_wildcard_host_and_ephemeral_port_remain_valid():
    proxy = load_proxy_module()

    top_level = proxy.validate_local_input_config({
        "listen_ip": "",
        "listen_port": 0,
    })
    explicit = proxy.validate_local_input_config({
        "input": {
            "type": "udp",
            "listen_ip": "",
            "listen_port": 0,
        }
    })

    assert top_level == explicit == {
        "type": "udp",
        "listen_ip": "",
        "listen_port": 0,
    }


@pytest.mark.parametrize("value", [False, True])
def test_udp_listen_port_rejects_booleans(value):
    proxy = load_proxy_module()
    configs = (
        {"listen_ip": "::", "listen_port": value},
        {
            "input": {
                "type": "udp",
                "listen_ip": "::",
                "listen_port": value,
            }
        },
    )

    for config in configs:
        with pytest.raises(proxy.ProxyConfigError, match="integer UDP port"):
            proxy.validate_local_input_config(config)


@pytest.mark.parametrize(
    "port",
    [
        "/dev/serial/by-id/usb-SRT_Marine_Technology_Ltd._AIS_Virtual_COM_Port_<device-id>-if00",
        "COM4",
    ],
)
def test_serial_port_string_is_passed_unchanged(port):
    proxy = load_proxy_module()
    config = serial_proxy_config(port=port)

    input_config = proxy.validate_local_input_config(config)

    assert input_config["type"] == "serial"
    assert input_config["port"] == port
    assert input_config["baudrate"] == 38400
    assert input_config["bytesize"] == 8
    assert input_config["parity"] == "N"
    assert input_config["stopbits"] == 1
    assert input_config["read_timeout"] == 1.0
    assert input_config["reconnect_delay"] == 5.0
    assert input_config["max_line_bytes"] == 4096


@pytest.mark.parametrize(
    ("input_config", "message"),
    [
        (None, "explicit null"),
        ({}, "input.type"),
        ({"type": "udp"}, "input.listen_ip"),
        (
            {
                "type": "udp",
                "listen_ip": "::",
                "listen_port": 50000,
                "port": "COM4",
            },
            "unknown UDP input option",
        ),
        (
            {"type": "tcp", "listen_ip": "::", "listen_port": 50000},
            "unsupported",
        ),
        ({"type": None}, "input.type"),
        ({"type": "serial"}, "input.port"),
        ({"type": "serial", "port": ""}, "input.port"),
        ({"type": "serial", "port": None}, "input.port"),
        ({"type": "serial", "port": "COM4", "baudrate": 0}, "baudrate"),
        ({"type": "serial", "port": "COM4", "baudrate": 38400.0}, "baudrate"),
        ({"type": "serial", "port": "COM4", "bytesize": 9}, "bytesize"),
        ({"type": "serial", "port": "COM4", "bytesize": None}, "bytesize"),
        ({"type": "serial", "port": "COM4", "parity": "X"}, "parity"),
        ({"type": "serial", "port": "COM4", "parity": None}, "parity"),
        ({"type": "serial", "port": "COM4", "stopbits": 1.2}, "stopbits"),
        ({"type": "serial", "port": "COM4", "stopbits": None}, "stopbits"),
        ({"type": "serial", "port": "COM4", "read_timeout": 0}, "read_timeout"),
        ({"type": "serial", "port": "COM4", "read_timeout": None}, "read_timeout"),
        (
            {"type": "serial", "port": "COM4", "reconnect_delay": 0},
            "reconnect_delay",
        ),
        (
            {"type": "serial", "port": "COM4", "max_line_bytes": 0},
            "max_line_bytes",
        ),
    ],
)
def test_invalid_local_input_config_is_rejected(input_config, message):
    proxy = load_proxy_module()

    with pytest.raises(proxy.ProxyConfigError, match=message):
        proxy.validate_local_input_config({"input": input_config})


def test_allow_from_is_rejected_with_serial_input():
    proxy = load_proxy_module()
    config = serial_proxy_config()
    config["allow_from"] = ["192.0.2.15"]

    with pytest.raises(proxy.ProxyConfigError, match="allow_from"):
        proxy.validate_local_input_config(config)


@pytest.mark.parametrize("option", ["listen_ip", "listen_port", "allow_from"])
def test_udp_only_nested_options_are_rejected_with_serial_input(option):
    proxy = load_proxy_module()
    value = {
        "listen_ip": "::",
        "listen_port": 50000,
        "allow_from": ["192.0.2.15"],
    }[option]
    config = serial_proxy_config(**{option: value})

    with pytest.raises(proxy.ProxyConfigError, match=option):
        proxy.validate_local_input_config(config)


def test_top_level_allow_from_is_rejected_with_explicit_udp_input():
    proxy = load_proxy_module()
    config = {
        "input": {
            "type": "udp",
            "listen_ip": "::",
            "listen_port": 50000,
        },
        "allow_from": ["2001:db8:42::/64"],
    }

    with pytest.raises(proxy.ProxyConfigError, match="use input.allow_from"):
        proxy.validate_local_input_config(config)


def test_source_ip_remains_valid_with_serial_input():
    proxy = load_proxy_module()
    config = serial_proxy_config()
    config["source_ip"] = "192.0.2.20"

    proxy.validate_local_input_config(config)
    source_address = proxy.parse_source_ip(config)

    assert str(source_address) == "192.0.2.20"


def test_line_framer_accepts_crlf_lf_and_cr():
    adapters = load_input_adapters()
    framer = adapters.SerialLineFramer(64)

    lines, dropped = framer.feed(b"one\r\ntwo\nthree\rfour")

    assert dropped == 0
    assert lines == [b"one", b"two", b"three"]
    assert framer.buffer_size == len(b"four")


def test_line_framer_handles_fragmented_lines_and_trailing_partial():
    adapters = load_input_adapters()
    framer = adapters.SerialLineFramer(64)

    assert framer.feed(b"!AIV")[0] == []
    lines, dropped = framer.feed(b"DM,1,1,,A,payload,0*00\r\npartial")

    assert dropped == 0
    assert lines == [b"!AIVDM,1,1,,A,payload,0*00"]
    assert framer.buffer_size == len(b"partial")


def test_line_framer_handles_several_lines_and_empty_lines():
    adapters = load_input_adapters()
    framer = adapters.SerialLineFramer(64)

    lines, dropped = framer.feed(b"one\n\ntwo\r\n")

    assert dropped == 0
    assert lines == [b"one", b"", b"two"]


def test_line_framer_discards_overlong_line_and_recovers():
    adapters = load_input_adapters()
    framer = adapters.SerialLineFramer(4)

    lines, dropped = framer.feed(b"abcde\nok\n")

    assert dropped == 1
    assert lines == [b"ok"]
    assert framer.buffer_size <= 4


class FakeSerial:
    def __init__(self, chunks=None, error=None):
        self.chunks = list(chunks or [])
        self.error = error
        self.closed = False
        self.read_count = 0

    def read(self, _size):
        self.read_count += 1
        if self.error:
            raise self.error
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True


def test_serial_adapter_opens_with_exact_configured_arguments():
    adapters = load_input_adapters()
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeSerial()

    settings = adapters.validate_serial_input_config({
        "type": "serial",
        "port": "COM4",
        "baudrate": 38400,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "read_timeout": 1.0,
        "reconnect_delay": 5,
        "max_line_bytes": 4096,
    })
    adapter = adapters.SerialInputAdapter(settings, serial_factory=factory)

    serial_obj = adapter._open_serial()

    assert isinstance(serial_obj, FakeSerial)
    assert calls == [{
        "port": "COM4",
        "baudrate": 38400,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1.0,
    }]


def test_serial_adapter_reader_queues_complete_lines_and_shutdown_closes_port():
    adapters = load_input_adapters()
    logs = []
    serial_obj = FakeSerial([
        b"!AIVDM,1,1,,A,payload,0*00\r",
        b"\n!AIVDO,1,1,,A,ownship,0*00\n",
    ])

    adapter = adapters.SerialInputAdapter(
        {
            "type": "serial",
            "port": "COM4",
            "read_timeout": 0.01,
            "reconnect_delay": 0.01,
        },
        serial_factory=lambda **_kwargs: serial_obj,
        logger=logs.append,
    )

    adapter.start()
    assert wait_for(lambda: adapter.queue.qsize() >= 2)
    adapter.close()

    assert serial_obj.closed
    assert not adapter._thread.is_alive()
    assert adapter.read_pending() == [
        b"!AIVDM,1,1,,A,payload,0*00",
        b"!AIVDO,1,1,,A,ownship,0*00",
    ]


def test_serial_adapter_reconnects_after_read_error_and_closes_old_port():
    adapters = load_input_adapters()
    first = FakeSerial(error=OSError("device vanished"))
    second = FakeSerial()
    created = [first, second]

    def factory(**_kwargs):
        return created.pop(0)

    adapter = adapters.SerialInputAdapter(
        {
            "type": "serial",
            "port": "COM4",
            "read_timeout": 0.01,
            "reconnect_delay": 0.01,
        },
        serial_factory=factory,
        logger=lambda _message: None,
    )

    adapter.start()
    assert wait_for(lambda: len(created) == 0)
    adapter.close()

    assert first.closed
    assert second.closed
    assert adapter.dropped_queue_lines == 0


def test_serial_adapter_initially_missing_device_retries():
    adapters = load_input_adapters()
    calls = []
    serial_obj = FakeSerial()

    def factory(**_kwargs):
        calls.append("open")
        if len(calls) == 1:
            raise OSError("no such port")
        return serial_obj

    adapter = adapters.SerialInputAdapter(
        {
            "type": "serial",
            "port": "COM4",
            "read_timeout": 0.01,
            "reconnect_delay": 0.01,
        },
        serial_factory=factory,
        logger=lambda _message: None,
    )

    adapter.start()
    assert wait_for(lambda: len(calls) >= 2)
    adapter.close()

    assert serial_obj.closed


def test_serial_adapter_bounded_queue_drops_oldest_line():
    adapters = load_input_adapters()
    logs = []
    adapter = adapters.SerialInputAdapter(
        {"type": "serial", "port": "COM4"},
        serial_factory=lambda **_kwargs: FakeSerial(),
        queue_max_lines=2,
        logger=logs.append,
    )

    adapter._enqueue_line(b"old")
    adapter._enqueue_line(b"middle")
    adapter._enqueue_line(b"new")

    assert adapter.dropped_queue_lines == 1
    assert adapter.read_pending() == [b"middle", b"new"]
    assert "dropped oldest queued line" in logs[0]


def test_serial_mode_does_not_create_udp_listener(monkeypatch):
    proxy = load_proxy_module()

    class FakeSerialInputAdapter:
        def __init__(self, input_config):
            self.input_config = input_config

    def fail_socket(*_args, **_kwargs):
        raise AssertionError("serial input must not create or bind UDP listener")

    monkeypatch.setattr(proxy.socket, "socket", fail_socket)
    monkeypatch.setattr(proxy, "SerialInputAdapter", FakeSerialInputAdapter)
    adapter = proxy.create_local_input_adapter(
        serial_proxy_config(),
        proxy.NetworkPolicy.unrestricted(),
    )

    assert isinstance(adapter, FakeSerialInputAdapter)
    assert adapter.input_config["type"] == "serial"


def test_serial_mode_missing_pyserial_fails_during_startup(monkeypatch):
    proxy = load_proxy_module()
    real_import = builtins.__import__

    def import_without_serial(name, *args, **kwargs):
        if name == "serial":
            raise ImportError("No module named serial")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_serial)

    with pytest.raises(proxy.ProxyConfigError, match="requires pySerial"):
        proxy.create_local_input_adapter(
            serial_proxy_config(),
            proxy.NetworkPolicy.unrestricted(),
        )


def test_udp_input_does_not_import_pyserial(monkeypatch):
    proxy = load_proxy_module()
    created = []
    real_import = builtins.__import__

    class FakeUdpSocket:
        def bind(self, addr):
            self.bound = addr

    def fake_socket(*_args, **_kwargs):
        sock = FakeUdpSocket()
        created.append(sock)
        return sock

    def import_without_serial(name, *args, **kwargs):
        if name == "serial":
            raise AssertionError("UDP input must not import pySerial")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(proxy.socket, "socket", fake_socket)
    monkeypatch.setattr(builtins, "__import__", import_without_serial)

    adapter = proxy.create_local_input_adapter(
        {"listen_ip": "127.0.0.1", "listen_port": 50000},
        proxy.NetworkPolicy.unrestricted(),
    )

    assert isinstance(adapter, proxy.UdpInputAdapter)
    assert created[0].bound == ("127.0.0.1", 50000)


def test_explicit_udp_adapter_binds_normalized_listener(monkeypatch):
    proxy = load_proxy_module()
    created = []

    class FakeUdpSocket:
        def bind(self, addr):
            self.bound = addr

    def fake_socket(*_args, **_kwargs):
        sock = FakeUdpSocket()
        created.append(sock)
        return sock

    monkeypatch.setattr(proxy.socket, "socket", fake_socket)

    adapter = proxy.create_local_input_adapter(
        {
            "input": {
                "type": "udp",
                "listen_ip": "127.0.0.2",
                "listen_port": 50123,
            },
            "listen_ip": "127.0.0.1",
            "listen_port": 50000,
        },
        proxy.NetworkPolicy.unrestricted(),
    )

    assert isinstance(adapter, proxy.UdpInputAdapter)
    assert created[0].bound == ("127.0.0.2", 50123)


def test_serial_payload_is_encrypted_through_existing_udpsec_format(monkeypatch):
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    session_key_material = proxy.SessionKeyMaterial(
        client_to_server_key=client_to_server_key,
        server_to_client_key=b"\x02" * 32,
    )
    remote_addr = ("192.0.2.10", 19999)
    timeouts = []

    class FakeSerialAdapter:
        def __init__(self):
            self.sent_payload = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.05

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            if self.sent_payload:
                return []
            self.sent_payload = True
            return [b"noise !AIVDM,1,1,,A,payload,0*00 trailer"]

    class OutSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, destination):
            self.sent.append((data, destination))

    out_sock = OutSocket()

    def fake_select(readable, _writable, _exceptional, timeout):
        assert readable == [out_sock]
        timeouts.append(timeout)
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    reason = proxy.forward_loop(
        FakeSerialAdapter(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        session_key_material,
        remote_addr,
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert timeouts == [0.05]
    assert len(out_sock.sent) == 1
    packet, destination = out_sock.sent[0]
    assert destination == remote_addr
    assert proxy.decrypt_secure_json_message(
        packet,
        client_to_server_key,
    ) == {
        "type": "nmea",
        "payload": "!AIVDM,1,1,,A,payload,0*00",
        "timestamp": 1000,
        "source_id": "boat_001",
    }


def test_serial_payload_with_non_ai_talker_is_encrypted_through_udpsec_format(
    monkeypatch,
):
    """Regression for Point 4: a base-station (non-AI) talker sentence must
    reach the encrypted UDPSEC NMEA payload unchanged through the real
    serial-input forwarding loop, not just at the extraction unit boundary."""
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    session_key_material = proxy.SessionKeyMaterial(
        client_to_server_key=client_to_server_key,
        server_to_client_key=b"\x02" * 32,
    )
    remote_addr = ("192.0.2.10", 19999)
    sentence = "!BSVDM,1,1,,A,payload,0*00"

    class FakeSerialAdapter:
        def __init__(self):
            self.sent_payload = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.05

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            if self.sent_payload:
                return []
            self.sent_payload = True
            return [f"noise {sentence} trailer".encode()]

    class OutSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, destination):
            self.sent.append((data, destination))

    out_sock = OutSocket()

    def fake_select(readable, _writable, _exceptional, timeout):
        assert readable == [out_sock]
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    reason = proxy.forward_loop(
        FakeSerialAdapter(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        session_key_material,
        remote_addr,
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert len(out_sock.sent) == 1
    packet, destination = out_sock.sent[0]
    assert destination == remote_addr
    assert proxy.decrypt_secure_json_message(
        packet,
        client_to_server_key,
    ) == {
        "type": "nmea",
        "payload": sentence,
        "timestamp": 1000,
        "source_id": "boat_001",
    }


def test_print_forwarding_heartbeat_labels_serial_input(capsys):
    """Regression for Point 7: the sparse heartbeat labels a real serial
    input adapter as input=serial, not the udp fallback."""
    proxy = load_proxy_module()
    adapter = proxy.SerialInputAdapter(
        {"type": "serial", "port": "COM4"},
        serial_factory=lambda **_kwargs: None,
    )
    stats = proxy.ForwardingStats()
    stats.record_forwarded("!AIVDM,1,1,,A,payload,0*00")

    proxy.print_forwarding_heartbeat(adapter, proxy.UDP_OUTPUT_TYPE, stats)

    captured = capsys.readouterr()
    assert "input=serial output=udp" in captured.out
    assert "forwarded=1 messages" in captured.out


def test_forward_loop_prints_sparse_heartbeat_with_session_state(
    monkeypatch,
    capsys,
):
    """Regression for Point 7: the UDPSEC loop reports a sparse,
    debug-independent heartbeat including session liveness, instead of
    tracing every forwarded sentence."""
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    session_key_material = proxy.SessionKeyMaterial(
        client_to_server_key=client_to_server_key,
        server_to_client_key=b"\x02" * 32,
    )
    remote_addr = ("192.0.2.10", 19999)

    class FakeAdapter:
        def __init__(self):
            self._served = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.0

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            if self._served:
                return []
            self._served = True
            return [b"!AIVDM,1,1,,A,payload,0*00"]

    class OutSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, destination):
            self.sent.append((data, destination))

    out_sock = OutSocket()
    clock = itertools.chain(
        [0.0, 0.0, 0.0, 0.0, 61.0],
        itertools.repeat(61.0),
    )
    monkeypatch.setattr(proxy.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    select_calls = []

    def fake_select(_readable, _writable, _exceptional, _timeout):
        select_calls.append(1)
        if len(select_calls) >= 2:
            raise OSError("end test")
        return ([], [], [])

    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.forward_loop(
        FakeAdapter(),
        out_sock,
        {
            "station_id": "boat_001",
            "keepalive_interval": 30,
            "peer_timeout": 90,
            "session_refresh_interval": 0,
        },
        session_key_material,
        remote_addr,
    )

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    # A keepalive ping may also fire at this clock tick; forwarding
    # correctness itself is covered elsewhere. Here only the heartbeat
    # matters.
    assert len(out_sock.sent) >= 1
    captured = capsys.readouterr()
    assert captured.out.count("Runtime:") == 1
    assert "input=udp output=udpsec" in captured.out
    assert "forwarded=1 messages" in captured.out
    assert "session=up" in captured.out


def test_forward_loop_stats_persist_across_successive_sessions(monkeypatch):
    """Regression for Point 7: ForwardingStats must survive UDPSEC
    reconnect/rekey. The same instance passed across two successive
    forward_loop() calls, exactly as main()'s reconnect loop does, must
    keep accumulating rather than resetting per session."""
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    session_key_material = proxy.SessionKeyMaterial(
        client_to_server_key=client_to_server_key,
        server_to_client_key=b"\x02" * 32,
    )
    remote_addr = ("192.0.2.10", 19999)
    shared_stats = proxy.ForwardingStats()

    class FakeAdapter:
        def __init__(self):
            self._served = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.0

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            if self._served:
                return []
            self._served = True
            return [b"!AIVDM,1,1,,A,payload,0*00"]

    class OutSocket:
        def sendto(self, _data, _destination):
            pass

    out_sock = OutSocket()
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 0,
    }

    monkeypatch.setattr(proxy.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    def fake_select(_readable, _writable, _exceptional, _timeout):
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)

    for _ in range(2):
        reason = proxy.forward_loop(
            FakeAdapter(),
            out_sock,
            config,
            session_key_material,
            remote_addr,
            None,
            shared_stats,
        )
        assert reason == proxy.SESSION_END_SOCKET_ERROR

    assert shared_stats.messages == 2


def test_forward_loop_heartbeat_schedule_persists_across_successive_sessions(
    monkeypatch,
    capsys,
):
    """Regression: the heartbeat deadline belongs to the relation (carried
    on the shared ForwardingStats), not to one session. Two successive
    sessions, each individually far shorter than HEARTBEAT_INTERVAL_SECONDS,
    must share one relation-level deadline rather than each restarting
    their own 60s countdown -- otherwise a session_refresh_interval shorter
    than the heartbeat interval could suppress the heartbeat indefinitely."""
    proxy = load_proxy_module()
    client_to_server_key = b"\x01" * 32
    session_key_material = proxy.SessionKeyMaterial(
        client_to_server_key=client_to_server_key,
        server_to_client_key=b"\x02" * 32,
    )
    remote_addr = ("192.0.2.10", 19999)
    shared_stats = proxy.ForwardingStats()
    config = {
        "station_id": "boat_001",
        "keepalive_interval": 300,
        "peer_timeout": 900,
        "session_refresh_interval": 0,
    }

    class FakeAdapter:
        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.0

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            return []

    class OutSocket:
        def sendto(self, _data, _destination):
            pass

    out_sock = OutSocket()
    monkeypatch.setattr(proxy.time, "time", lambda: 1000)

    def fake_select(_readable, _writable, _exceptional, _timeout):
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)

    # Session 1's relation clock never advances past 0; its first
    # is_heartbeat_due() check establishes the relation deadline at
    # 0 + 60 = 60 and the session ends immediately after, well short of
    # its own 60s lifetime.
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 0.0)
    reason1 = proxy.forward_loop(
        FakeAdapter(),
        out_sock,
        config,
        session_key_material,
        remote_addr,
        None,
        shared_stats,
    )
    assert reason1 == proxy.SESSION_END_SOCKET_ERROR
    assert "Runtime:" not in capsys.readouterr().out

    # Session 2 starts at t=65: past the ORIGINAL relation deadline (60)
    # established in session 1, but a fresh per-session deadline computed
    # from this session's own start (65 + 60 = 125) would not have fired.
    # The heartbeat firing here proves the deadline persisted from session
    # 1 rather than resetting.
    monkeypatch.setattr(proxy.time, "monotonic", lambda: 65.0)
    reason2 = proxy.forward_loop(
        FakeAdapter(),
        out_sock,
        config,
        session_key_material,
        remote_addr,
        None,
        shared_stats,
    )
    assert reason2 == proxy.SESSION_END_SOCKET_ERROR
    assert "Runtime:" in capsys.readouterr().out
