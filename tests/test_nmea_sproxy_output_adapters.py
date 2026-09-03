import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec


ROOT = Path(__file__).resolve().parents[1]
NMEA_SPROXY_DIR = ROOT / "nmea_sproxy"


def load_proxy_module():
    previous_meta_cleaner = sys.modules.pop("meta_cleaner", None)
    sys.path.insert(0, str(NMEA_SPROXY_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "nmea_sproxy_output_tests", NMEA_SPROXY_DIR / "nmea_sproxy.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(NMEA_SPROXY_DIR))
        sys.modules.pop("meta_cleaner", None)
        if previous_meta_cleaner is not None:
            sys.modules["meta_cleaner"] = previous_meta_cleaner


def load_output_adapters():
    spec = importlib.util.spec_from_file_location(
        "nmea_sproxy_output_adapters_tests",
        NMEA_SPROXY_DIR / "output_adapters.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_config(**overrides):
    config = {
        "remote_host": "mixer.example.net",
        "remote_port": 19999,
    }
    config.update(overrides)
    return config


def _install_udpsec_main_fakes(monkeypatch, proxy, events):
    remote_addr = ("192.0.2.10", 19999)
    key_material = SimpleNamespace(
        client_to_server_key=b"\x11" * 32,
        server_to_client_key=b"\x22" * 32,
    )
    config = {
        "input": {
            "type": "udp",
            "listen_ip": "127.0.0.1",
            "listen_port": 50000,
        },
        "output": {
            "type": "udpsec",
            "host": remote_addr[0],
            "port": remote_addr[1],
            "legacy": False,
        },
        "station_id": "boat_001",
        "station_private_key": "station.pem",
        "remote_public_key": "server.pem",
        "reconnect_delay": 5,
        "keepalive_interval": 30,
        "peer_timeout": 90,
        "session_refresh_interval": 0,
    }

    class OutSocket:
        def settimeout(self, timeout):
            events.append(("timeout", timeout))

        def sendto(self, packet, destination):
            events.append(("send", packet, destination))

        def close(self):
            events.append(("out-close",))

    class LocalInput:
        def start(self):
            events.append(("input-start",))

        def close(self):
            events.append(("input-close",))

    out_socket = OutSocket()
    local_input = LocalInput()
    station_identity = SimpleNamespace(
        private_key=object(),
        generated=False,
        private_path=Path("station.pem"),
        public_path=Path("station-public.pem"),
    )

    monkeypatch.setattr(proxy, "resolve_config_path", lambda _path=None: None)
    monkeypatch.setattr(proxy, "load_config", lambda _path=None: config)
    monkeypatch.setattr(
        proxy,
        "resolve_output_endpoint",
        lambda _output: (remote_addr, proxy.socket.AF_INET),
    )
    monkeypatch.setattr(
        proxy,
        "ensure_station_identity",
        lambda _config: station_identity,
    )
    monkeypatch.setattr(proxy, "load_peer_public_key", lambda _path: object())
    monkeypatch.setattr(
        proxy,
        "create_output_socket",
        lambda _output, _family: out_socket,
    )
    monkeypatch.setattr(
        proxy,
        "create_local_input_adapter",
        lambda _config, _policy: local_input,
    )
    monkeypatch.setattr(
        proxy,
        "perform_handshake",
        lambda *_args: key_material,
    )
    return config, remote_addr, key_material


def test_output_omitted_preserves_legacy_udpsec_behavior():
    proxy = load_proxy_module()
    config = legacy_config(source_ip="192.0.2.20")

    output = proxy.validate_output_config(config)

    assert output == {
        "type": "udpsec",
        "host": "mixer.example.net",
        "port": 19999,
        "source_ip": "192.0.2.20",
        "legacy": True,
    }
    assert config["output"] == output


def test_valid_explicit_udpsec_output_takes_precedence_over_legacy_fields():
    proxy = load_proxy_module()
    config = legacy_config(
        remote_host="legacy.example.net",
        remote_port=19999,
        source_ip="192.0.2.10",
        output={
            "type": "udpsec",
            "host": "explicit.example.net",
            "port": 20000,
            "source_ip": "192.0.2.20",
        },
    )

    output = proxy.validate_output_config(config)

    assert output == {
        "type": "udpsec",
        "host": "explicit.example.net",
        "port": 20000,
        "source_ip": "192.0.2.20",
        "legacy": False,
    }


def test_valid_plain_udp_output():
    proxy = load_proxy_module()
    config = legacy_config(
        output={
            "type": "udp",
            "host": "192.168.10.20",
            "port": 17777,
            "source_ip": "192.168.10.15",
        },
    )

    output = proxy.validate_output_config(config)

    assert output["type"] == "udp"
    assert output["host"] == "192.168.10.20"
    assert output["port"] == 17777
    assert output["source_ip"] == "192.168.10.15"
    assert output["legacy"] is False


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (None, "output"),
        ({}, "output.type"),
        ({"type": None}, "output.type"),
        ({"type": "tcp", "host": "example.net", "port": 1}, "output.type"),
        ({"type": "UDP", "host": "example.net", "port": 1}, "output.type"),
        ({"type": "udp", "port": 1}, "output.host"),
        ({"type": "udp", "host": "", "port": 1}, "output.host"),
        ({"type": "udp", "host": None, "port": 1}, "output.host"),
        ({"type": "udp", "host": "example.net"}, "output.port"),
        ({"type": "udp", "host": "example.net", "port": 0}, "output.port"),
        ({"type": "udp", "host": "example.net", "port": 65536}, "output.port"),
        ({"type": "udp", "host": "example.net", "port": "17777"}, "output.port"),
        (
            {
                "type": "udp",
                "host": "example.net",
                "port": 17777,
                "source_ip": "not an ip",
            },
            "output.source_ip",
        ),
        (
            {
                "type": "udp",
                "host": "example.net",
                "port": 17777,
                "source_ip": None,
            },
            "output.source_ip",
        ),
        (
            {
                "type": "udp",
                "host": "example.net",
                "port": 17777,
                "extra": True,
            },
            "output.extra",
        ),
    ],
)
def test_invalid_explicit_output_config_is_rejected(output, message):
    proxy = load_proxy_module()

    with pytest.raises(proxy.ProxyConfigError, match=message):
        proxy.validate_output_config(legacy_config(output=output))


def test_explicit_output_does_not_mix_missing_port_from_legacy_field():
    proxy = load_proxy_module()

    with pytest.raises(proxy.ProxyConfigError, match="output.port"):
        proxy.validate_output_config(
            legacy_config(
                remote_port=19999,
                output={"type": "udp", "host": "192.168.10.20"},
            )
        )


def test_output_family_mismatch_is_rejected():
    adapters = load_output_adapters()
    output = {
        "type": "udp",
        "host": "2001:db8::20",
        "port": 17777,
        "source_ip": "192.0.2.15",
        "legacy": False,
    }

    with pytest.raises(adapters.OutputConfigError, match="output.source_ip"):
        adapters.resolve_output_endpoint(output)


class FakeSocket:
    def __init__(self, family=None, sock_type=None, bind_error=None):
        self.family = family
        self.sock_type = sock_type
        self.bind_error = bind_error
        self.bound = None
        self.closed = False
        self.sent = []

    def bind(self, addr):
        if self.bind_error:
            raise self.bind_error
        self.bound = addr

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


def test_plain_udp_source_ip_binds_output_socket(monkeypatch):
    adapters = load_output_adapters()
    created = []

    def fake_socket(family, sock_type):
        sock = FakeSocket(family, sock_type)
        created.append(sock)
        return sock

    monkeypatch.setattr(adapters.socket, "socket", fake_socket)
    output = {
        "type": "udp",
        "host": "192.168.10.20",
        "port": 17777,
        "source_ip": "192.168.10.15",
        "legacy": False,
    }

    sock = adapters.create_output_socket(output, adapters.socket.AF_INET)

    assert sock is created[0]
    assert sock.bound == ("192.168.10.15", 0)


def test_plain_udp_omitted_source_ip_leaves_output_socket_unbound(monkeypatch):
    adapters = load_output_adapters()
    created = []

    def fake_socket(family, sock_type):
        sock = FakeSocket(family, sock_type)
        created.append(sock)
        return sock

    monkeypatch.setattr(adapters.socket, "socket", fake_socket)
    output = {
        "type": "udp",
        "host": "192.168.10.20",
        "port": 17777,
        "legacy": False,
    }

    sock = adapters.create_output_socket(output, adapters.socket.AF_INET)

    assert sock is created[0]
    assert sock.bound is None


def test_plain_udp_hostname_resolution_is_constrained_by_source_family(monkeypatch):
    adapters = load_output_adapters()
    calls = []

    def fake_getaddrinfo(host, port, family, sock_type):
        calls.append((host, port, family, sock_type))
        return [(family, sock_type, 17, "", ("2001:db8::20", port, 0, 0))]

    monkeypatch.setattr(adapters.socket, "getaddrinfo", fake_getaddrinfo)
    output = {
        "type": "udp",
        "host": "mixer.example.net",
        "port": 17777,
        "source_ip": "2001:db8::15",
        "legacy": False,
    }

    remote_addr, family = adapters.resolve_output_endpoint(output)

    assert family == adapters.socket.AF_INET6
    assert calls == [
        (
            "mixer.example.net",
            17777,
            adapters.socket.AF_INET6,
            adapters.socket.SOCK_DGRAM,
        )
    ]
    assert remote_addr == ("2001:db8::20", 17777, 0, 0)


def test_plain_udp_send_uses_exact_destination_tuple():
    adapters = load_output_adapters()
    sock = FakeSocket()
    adapter = adapters.PlainUdpOutputAdapter(sock, ("192.168.10.20", 17777))

    adapter.send_sentence("!AIVDM,1,1,,A,payload,0*00")

    assert sock.sent == [
        (b"!AIVDM,1,1,,A,payload,0*00", ("192.168.10.20", 17777))
    ]


def test_plain_udp_payload_sends_aivdm_and_aivdo_as_unencrypted_datagrams():
    proxy = load_proxy_module()
    adapters = load_output_adapters()
    sock = FakeSocket()
    adapter = adapters.PlainUdpOutputAdapter(sock, ("192.168.10.20", 17777))

    proxy.forward_input_payload(
        b"noise !AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C trailer",
        adapter.send_sentence,
    )
    proxy.forward_input_payload(
        b"!AIVDO,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*42",
        adapter.send_sentence,
    )

    assert sock.sent == [
        (
            b"!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C",
            ("192.168.10.20", 17777),
        ),
        (
            b"!AIVDO,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*42",
            ("192.168.10.20", 17777),
        ),
    ]
    for datagram, _addr in sock.sent:
        assert not datagram.startswith(proxy.DATA_PREFIX)
        assert b"{" not in datagram
        assert b"boat_001" not in datagram


def test_plain_udp_payload_ignores_unrelated_noise():
    proxy = load_proxy_module()
    adapters = load_output_adapters()
    sock = FakeSocket()
    adapter = adapters.PlainUdpOutputAdapter(sock, ("192.168.10.20", 17777))

    proxy.forward_input_payload(b"not AIS data", adapter.send_sentence)

    assert sock.sent == []


def test_plain_udp_payload_does_not_rewrite_tag_metadata():
    proxy = load_proxy_module()
    adapters = load_output_adapters()
    sock = FakeSocket()
    adapter = adapters.PlainUdpOutputAdapter(sock, ("192.168.10.20", 17777))
    payload = b"\\s:rx,c:1234\\!AIVDM,1,1,,A,payload,0*00"

    proxy.forward_input_payload(payload, adapter.send_sentence)

    assert sock.sent == [
        (b"!AIVDM,1,1,,A,payload,0*00", ("192.168.10.20", 17777))
    ]


def test_udp_input_to_plain_udp_uses_allow_from(monkeypatch):
    proxy = load_proxy_module()
    policy = proxy.NetworkPolicy.from_entries(
        ["192.0.2.15"],
        context="nmea_sproxy.allow_from",
    )

    class LocalSocket:
        def recvfrom(self, _size):
            return b"!AIVDM,1,1,,A,payload,0*00", ("192.0.2.15", 50000)

    class Output:
        def __init__(self):
            self.sent = []

        def send_sentence(self, line):
            self.sent.append(line)

    local_sock = LocalSocket()
    output = Output()
    select_calls = []

    def fake_select(_readable, _writable, _exceptional, _timeout):
        if not select_calls:
            select_calls.append("local")
            return [local_sock], [], []
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.plain_udp_forward_loop(local_sock, output, policy)

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert output.sent == ["!AIVDM,1,1,,A,payload,0*00"]


def test_udp_input_to_plain_udp_forwards_non_ai_talker(monkeypatch):
    """Regression for Point 4: a base-station (non-AI) talker sentence must
    reach plain UDP output unchanged through the real UDP-input forwarding
    loop, not just at the extraction unit boundary."""
    proxy = load_proxy_module()
    sentence = "!BSVDM,1,1,,A,payload,0*00"

    class LocalSocket:
        def recvfrom(self, _size):
            return sentence.encode(), ("192.0.2.15", 50000)

    class Output:
        def __init__(self):
            self.sent = []

        def send_sentence(self, line):
            self.sent.append(line)

    local_sock = LocalSocket()
    output = Output()
    select_calls = []

    def fake_select(_readable, _writable, _exceptional, _timeout):
        if not select_calls:
            select_calls.append("local")
            return [local_sock], [], []
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.plain_udp_forward_loop(local_sock, output)

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert output.sent == [sentence]


def test_udp_input_denied_before_plain_udp_send(monkeypatch):
    proxy = load_proxy_module()
    policy = proxy.NetworkPolicy.from_entries(
        ["198.51.100.0/24"],
        context="nmea_sproxy.allow_from",
    )

    class UndecodablePayload:
        def decode(self, *_args, **_kwargs):
            raise AssertionError("denied payload must not be decoded")

    class LocalSocket:
        def recvfrom(self, _size):
            return UndecodablePayload(), ("192.0.2.15", 50000)

    class Output:
        def __init__(self):
            self.sent = []

        def send_sentence(self, line):
            self.sent.append(line)

    local_sock = LocalSocket()
    output = Output()
    select_calls = []

    def fake_select(_readable, _writable, _exceptional, _timeout):
        if not select_calls:
            select_calls.append("local")
            return [local_sock], [], []
        raise OSError("end test")

    monkeypatch.setattr(proxy.select, "select", fake_select)

    reason = proxy.plain_udp_forward_loop(local_sock, output, policy)

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert output.sent == []


def test_serial_input_to_plain_udp_uses_queue_without_selecting_serial():
    proxy = load_proxy_module()

    class SerialAdapter:
        def __init__(self):
            self.sent = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.05

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            if self.sent:
                return []
            self.sent = True
            return [b"!AIVDM,1,1,,A,payload,0*00"]

    class Output:
        def __init__(self):
            self.sent = []

        def send_sentence(self, line):
            self.sent.append(line)
            raise OSError("end test")

    output = Output()

    reason = proxy.plain_udp_forward_loop(SerialAdapter(), output)

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert output.sent == ["!AIVDM,1,1,,A,payload,0*00"]


def test_serial_input_to_plain_udp_forwards_non_ai_talker():
    """Regression for Point 4: a base-station (non-AI) VDO sentence must
    reach plain UDP output unchanged through the real serial-input
    forwarding loop, not just at the extraction unit boundary."""
    proxy = load_proxy_module()
    sentence = "!BSVDO,1,1,,A,payload,0*00"

    class SerialAdapter:
        def __init__(self):
            self.sent = False

        def selectable_sockets(self):
            return []

        def poll_interval(self):
            return 0.05

        def read_ready(self, _ready_socket):
            return []

        def read_pending(self):
            if self.sent:
                return []
            self.sent = True
            return [sentence.encode()]

    class Output:
        def __init__(self):
            self.sent = []

        def send_sentence(self, line):
            self.sent.append(line)
            raise OSError("end test")

    output = Output()

    reason = proxy.plain_udp_forward_loop(SerialAdapter(), output)

    assert reason == proxy.SESSION_END_SOCKET_ERROR
    assert output.sent == [sentence]


def test_plain_udp_reconnect_reuses_pinned_resolved_destination(monkeypatch):
    proxy = load_proxy_module()
    adapters = sys.modules[proxy.PlainUdpOutputAdapter.__module__]
    output_config = {
        "type": "udp",
        "host": "mixer.example.net",
        "port": 17777,
        "source_ip": "192.0.2.15",
        "legacy": False,
    }
    first_addr = ("192.0.2.10", 17777)
    second_addr = ("192.0.2.11", 17777)
    resolutions = []
    sockets = []
    calls = []
    payload = "!AIVDM,1,1,,A,payload,0*00"

    def fake_getaddrinfo(host, port, family, sock_type):
        resolutions.append((host, port, family, sock_type))
        addr = first_addr if len(resolutions) == 1 else second_addr
        return [(family, sock_type, 17, "", addr)]

    def fake_socket(family, sock_type):
        sock = FakeSocket(family, sock_type)
        sockets.append(sock)
        return sock

    def fake_forward_loop(_local_input, output_adapter, _ingress_policy):
        calls.append(output_adapter.sock)
        output_adapter.send_sentence(payload)
        if len(calls) > 1:
            raise KeyboardInterrupt
        return proxy.SESSION_END_SOCKET_ERROR

    monkeypatch.setattr(adapters.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(adapters.socket, "socket", fake_socket)
    monkeypatch.setattr(proxy, "plain_udp_forward_loop", fake_forward_loop)
    monkeypatch.setattr(proxy.time, "sleep", lambda _delay: None)
    output_adapter = proxy.create_plain_udp_output_adapter(output_config)

    with pytest.raises(KeyboardInterrupt):
        proxy.run_plain_udp_relation(
            object(),
            output_config,
            {"reconnect_delay": 5},
            output_adapter=output_adapter,
        )

    assert resolutions == [
        (
            "mixer.example.net",
            17777,
            adapters.socket.AF_INET,
            adapters.socket.SOCK_DGRAM,
        )
    ]
    assert len(sockets) == 2
    assert len(calls) == 2
    assert calls == sockets
    assert sockets[0].closed
    assert sockets[1].closed
    assert sockets[0].bound == ("192.0.2.15", 0)
    assert sockets[1].bound == ("192.0.2.15", 0)
    assert sockets[0].sent == [(payload.encode(), first_addr)]
    assert sockets[1].sent == [(payload.encode(), first_addr)]


def test_plain_udp_main_does_not_open_keys_or_handshake(monkeypatch, tmp_path):
    proxy = load_proxy_module()
    keys_dir = tmp_path / "keys"
    station_private = keys_dir / "station_private.pem"
    station_public = keys_dir / "station_public.pem"
    missing_peer = keys_dir / "aismixer_public.pem"
    monkeypatch.setitem(
        proxy.DEFAULT_CONFIG,
        "station_private_key",
        str(station_private),
    )
    monkeypatch.setitem(
        proxy.DEFAULT_CONFIG,
        "remote_public_key",
        str(missing_peer),
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PRIVATE_KEY_PATH",
        str(station_private),
    )
    monkeypatch.setattr(
        proxy,
        "CANONICAL_STATION_PUBLIC_KEY_PATH",
        str(station_public),
    )
    config_path = tmp_path / "plain.yaml"
    config_path.write_text(
        "input:\n"
        "  type: udp\n"
        "  listen_ip: '127.0.0.1'\n"
        "  listen_port: 50000\n"
        "output:\n"
        "  type: udp\n"
        "  host: 192.168.10.20\n"
        "  port: 17777\n",
        encoding="utf-8",
    )

    class LocalInput:
        def start(self):
            pass

        def close(self):
            pass

    class PlainOutput:
        def close(self):
            pass

    def fail_key_load(_path):
        raise AssertionError("plain UDP must not load key files")

    def fail_handshake(*_args, **_kwargs):
        raise AssertionError("plain UDP must not perform UDPSEC handshake")

    def fail_identity_ensure(_config):
        raise AssertionError("plain UDP must not ensure station identity")

    def fail_peer_load(_path):
        raise AssertionError("plain UDP must not load peer trust")

    monkeypatch.setattr(proxy, "load_private_key", fail_key_load)
    monkeypatch.setattr(proxy, "load_public_key", fail_key_load)
    monkeypatch.setattr(proxy, "ensure_station_identity", fail_identity_ensure)
    monkeypatch.setattr(proxy, "load_peer_public_key", fail_peer_load)
    monkeypatch.setattr(proxy, "perform_handshake", fail_handshake)
    monkeypatch.setattr(
        proxy,
        "create_local_input_adapter",
        lambda _config, _policy: LocalInput(),
    )
    monkeypatch.setattr(
        proxy,
        "create_plain_udp_output_adapter",
        lambda _output: PlainOutput(),
    )
    monkeypatch.setattr(
        proxy,
        "run_plain_udp_relation",
        lambda *_args, **_kwargs: 0,
    )

    assert proxy.main(["--config", str(config_path)]) == 0
    assert not keys_dir.exists()
    assert not station_private.exists()
    assert not station_public.exists()
    assert not missing_peer.exists()


def test_explicit_udp_main_passes_nested_allow_from_policy_to_adapter(monkeypatch):
    proxy = load_proxy_module()
    captured = {}
    config = {
        "input": {
            "type": "udp",
            "listen_ip": "::",
            "listen_port": 50000,
            "allow_from": ["2001:db8:42::/64"],
        },
        "output": {
            "type": "udp",
            "host": "192.0.2.10",
            "port": 17777,
            "legacy": False,
        },
    }

    class LocalInput:
        def start(self):
            pass

        def close(self):
            pass

    class PlainOutput:
        def close(self):
            pass

    def capture_input(_config, policy):
        captured["policy"] = policy
        return LocalInput()

    monkeypatch.setattr(proxy, "resolve_config_path", lambda _path=None: None)
    monkeypatch.setattr(proxy, "load_config", lambda _path=None: config)
    monkeypatch.setattr(proxy, "create_local_input_adapter", capture_input)
    monkeypatch.setattr(
        proxy,
        "create_plain_udp_output_adapter",
        lambda _output: PlainOutput(),
    )
    monkeypatch.setattr(
        proxy,
        "run_plain_udp_relation",
        lambda *_args, **_kwargs: 0,
    )

    assert proxy.main([]) == 0
    assert captured["policy"].allows("2001:db8:42::15")
    assert not captured["policy"].allows("2001:db8:43::15")


def test_legacy_udpsec_main_ensures_identity_and_loads_peer_trust(
    monkeypatch,
    tmp_path,
    capsys,
):
    proxy = load_proxy_module()
    config_path = tmp_path / "udpsec.yaml"
    config_path.write_text(
        "listen_ip: '127.0.0.1'\n"
        "listen_port: 50000\n"
        "remote_host: 192.0.2.10\n"
        "remote_port: 19999\n"
        "station_private_key: station.pem\n"
        "remote_public_key: server.pem\n",
        encoding="utf-8",
    )
    calls = []
    station_private_key = ec.generate_private_key(ec.SECP256R1())
    server_public_key = ec.generate_private_key(ec.SECP256R1()).public_key()

    class OutSocket:
        def settimeout(self, _timeout):
            pass

        def close(self):
            pass

    class LocalInput:
        def start(self):
            pass

        def close(self):
            pass

    def ensure_station_identity(config):
        calls.append(("identity", config["output"]["type"], config["station_private_key"]))
        return SimpleNamespace(
            private_key=station_private_key,
            generated=False,
            private_path=Path(config["station_private_key"]),
            public_path=None,
        )

    def load_peer_public_key(path):
        calls.append(("peer", path))
        return server_public_key

    monkeypatch.setattr(proxy, "ensure_station_identity", ensure_station_identity)
    monkeypatch.setattr(proxy, "load_peer_public_key", load_peer_public_key)
    monkeypatch.setattr(
        proxy,
        "create_output_socket",
        lambda _output, _family: OutSocket(),
    )
    monkeypatch.setattr(
        proxy,
        "create_local_input_adapter",
        lambda _config, _policy: LocalInput(),
    )
    monkeypatch.setattr(proxy, "perform_handshake", lambda *_args: None)
    monkeypatch.setattr(
        proxy.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        proxy.main(["--config", str(config_path)])

    assert len(calls) == 2
    assert calls[0][0:2] == ("identity", "udpsec")
    assert calls[0][2].endswith("station.pem")
    assert calls[1][0] == "peer"
    assert calls[1][1].endswith("server.pem")
    assert capsys.readouterr().err.splitlines() == [
        proxy.LEGACY_INPUT_DEPRECATION_MESSAGE,
        proxy.LEGACY_OUTPUT_DEPRECATION_MESSAGE,
    ]


def test_udpsec_main_reports_missing_peer_trust_before_activation(
    monkeypatch,
    tmp_path,
    capsys,
):
    proxy = load_proxy_module()
    config_path = tmp_path / "udpsec.yaml"
    missing_peer = tmp_path / "trust" / "missing-server.pem"
    config_path.write_text(
        "input:\n"
        "  type: udp\n"
        "  listen_ip: '127.0.0.1'\n"
        "  listen_port: 50000\n"
        "station_private_key: operator-station.pem\n"
        "remote_public_key: trust/missing-server.pem\n"
        "output:\n"
        "  type: udpsec\n"
        "  host: 192.0.2.10\n"
        "  port: 19999\n",
        encoding="utf-8",
    )
    station_private_key = ec.generate_private_key(ec.SECP256R1())
    identity_calls = []

    def ensure_station_identity(config):
        identity_calls.append(config["output"]["type"])
        return SimpleNamespace(
            private_key=station_private_key,
            generated=False,
            private_path=Path(config["station_private_key"]),
            public_path=None,
        )

    def fail_activation(*_args, **_kwargs):
        raise AssertionError("missing peer trust must prevent runtime activation")

    monkeypatch.setattr(proxy, "ensure_station_identity", ensure_station_identity)
    monkeypatch.setattr(
        proxy,
        "resolve_output_endpoint",
        lambda _output: (("192.0.2.10", 19999), proxy.socket.AF_INET),
    )
    monkeypatch.setattr(proxy, "create_output_socket", fail_activation)
    monkeypatch.setattr(proxy, "create_local_input_adapter", fail_activation)

    assert proxy.main(["--config", str(config_path)]) == 1

    captured = capsys.readouterr()
    assert identity_calls == ["udpsec"]
    assert "public key" in captured.err.lower()
    assert str(missing_peer).lower() in captured.err.lower()


def test_plain_udp_main_does_not_process_udpsec_packets_or_send_ping(
    monkeypatch,
    tmp_path,
):
    proxy = load_proxy_module()
    config_path = tmp_path / "plain.yaml"
    config_path.write_text(
        "input:\n"
        "  type: udp\n"
        "  listen_ip: '127.0.0.1'\n"
        "  listen_port: 50000\n"
        "output:\n"
        "  type: udp\n"
        "  host: 192.168.10.20\n"
        "  port: 17777\n",
        encoding="utf-8",
    )

    class LocalInput:
        def start(self):
            pass

        def close(self):
            pass

    class PlainOutput:
        def close(self):
            pass

    monkeypatch.setattr(proxy, "handle_server_packet", lambda *_args: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(proxy, "send_ping", lambda *_args: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(proxy, "create_local_input_adapter", lambda _config, _policy: LocalInput())
    monkeypatch.setattr(proxy, "create_plain_udp_output_adapter", lambda _output: PlainOutput())
    monkeypatch.setattr(proxy, "run_plain_udp_relation", lambda *_args, **_kwargs: 0)

    assert proxy.main(["--config", str(config_path)]) == 0


def test_udpsec_main_sends_one_authenticated_close_before_socket_close(
    monkeypatch,
):
    proxy = load_proxy_module()
    events = []
    config, remote_addr, key_material = _install_udpsec_main_fakes(
        monkeypatch,
        proxy,
        events,
    )
    monkeypatch.setattr(proxy.time, "time", lambda: 1234)

    def interrupt_active_session(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(proxy, "forward_loop", interrupt_active_session)

    with pytest.raises(KeyboardInterrupt):
        proxy.main([])

    sends = [event for event in events if event[0] == "send"]
    assert len(sends) == 1
    _, packet, destination = sends[0]
    assert destination == remote_addr
    assert proxy.decrypt_secure_json_message(
        packet,
        key_material.client_to_server_key,
    ) == {
        "type": "close",
        "reason": "shutdown",
        "timestamp": 1234,
        "source_id": config["station_id"],
    }
    assert events.index(sends[0]) < events.index(("out-close",))
    assert events.index(("input-close",)) < events.index(("out-close",))


def test_udpsec_peer_close_clears_active_handle_and_uses_reconnect_delay(
    monkeypatch,
):
    proxy = load_proxy_module()
    events = []
    config, _, _ = _install_udpsec_main_fakes(
        monkeypatch,
        proxy,
        events,
    )
    monkeypatch.setattr(
        proxy,
        "forward_loop",
        lambda *_args, **_kwargs: proxy.SESSION_END_PEER_GRACEFUL_CLOSE,
    )

    def stop_after_backoff(delay):
        events.append(("sleep", delay))
        raise KeyboardInterrupt

    monkeypatch.setattr(proxy.time, "sleep", stop_after_backoff)

    with pytest.raises(KeyboardInterrupt):
        proxy.main([])

    assert ("sleep", config["reconnect_delay"]) in events
    assert not [event for event in events if event[0] == "send"]
    assert events[-2:] == [("input-close",), ("out-close",)]


def test_udpsec_main_proactive_rekey_is_immediate_then_failure_backs_off(
    monkeypatch,
):
    proxy = load_proxy_module()
    socket_events = []
    config, _, key_material = _install_udpsec_main_fakes(
        monkeypatch,
        proxy,
        socket_events,
    )
    lifecycle = []
    handshake_results = iter((key_material, None))

    def perform_handshake(*_args):
        lifecycle.append("handshake")
        return next(handshake_results)

    def forward_loop(*_args, **_kwargs):
        lifecycle.append("forward")
        return proxy.SESSION_END_PROACTIVE_REKEY

    def stop_after_backoff(delay):
        lifecycle.append(("sleep", delay))
        raise KeyboardInterrupt

    monkeypatch.setattr(proxy, "perform_handshake", perform_handshake)
    monkeypatch.setattr(proxy, "forward_loop", forward_loop)
    monkeypatch.setattr(proxy.time, "sleep", stop_after_backoff)

    with pytest.raises(KeyboardInterrupt):
        proxy.main([])

    assert lifecycle == [
        "handshake",
        "forward",
        "handshake",
        ("sleep", config["reconnect_delay"]),
    ]


def test_proxy_run_service_installs_and_restores_sigterm_handler(
    monkeypatch,
):
    proxy = load_proxy_module()
    previous_handler = object()
    signal_calls = []

    def fake_signal(signum, handler):
        signal_calls.append((signum, handler))
        return previous_handler

    def interrupt(_argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(proxy.signal, "signal", fake_signal)
    monkeypatch.setattr(proxy, "main", interrupt)

    with pytest.raises(KeyboardInterrupt):
        proxy.run_service(["--config", "unused.yaml"])

    assert signal_calls == [
        (proxy.signal.SIGTERM, proxy._raise_keyboard_interrupt_for_sigterm),
        (proxy.signal.SIGTERM, previous_handler),
    ]
    with pytest.raises(KeyboardInterrupt):
        proxy._raise_keyboard_interrupt_for_sigterm(
            proxy.signal.SIGTERM,
            None,
        )
