import select
import socket
from contextlib import ExitStack, closing

import pytest

import core.udp_listener as udp_listener


NETWORK_TIMEOUT = 2.0
ISOLATION_TIMEOUT = 0.05


class _RecordingSocket:
    def __init__(self, option_failure=None):
        self.option_failure = option_failure
        self.setsockopt_calls = []
        self.close_count = 0

    def setsockopt(self, level, option, value):
        self.setsockopt_calls.append((level, option, value))
        if self.option_failure is not None:
            raise self.option_failure

    def close(self):
        self.close_count += 1


def _install_recording_socket_factory(
    monkeypatch,
    *,
    option_failure=None,
):
    created = []

    def socket_factory(family, socket_type):
        sock = _RecordingSocket(option_failure=option_failure)
        created.append((family, socket_type, sock))
        return sock

    monkeypatch.setattr(udp_listener.socket, "socket", socket_factory)
    return created


def _require_ipv6_constants():
    required = (
        "AF_INET6",
        "IPPROTO_IPV6",
        "IPV6_V6ONLY",
    )
    if not all(hasattr(socket, name) for name in required):
        pytest.skip("Python has no required IPv6 socket constants")


def _require_ipv6_loopback():
    _require_ipv6_constants()
    if not socket.has_ipv6:
        pytest.skip("IPv6 loopback unavailable: Python has no IPv6 support")

    probe = None
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        probe.bind(("::1", 0))
    except OSError as exc:
        pytest.skip(f"IPv6 loopback unavailable: {exc}")
    finally:
        if probe is not None:
            probe.close()


@pytest.mark.parametrize("reuse_address", (False, True))
def test_ipv6_listener_explicitly_sets_v6only(
    monkeypatch,
    reuse_address,
):
    _require_ipv6_constants()
    created = _install_recording_socket_factory(monkeypatch)

    sock = udp_listener.create_udp_listener_socket(
        "::",
        reuse_address=reuse_address,
    )
    try:
        assert len(created) == 1
        family, socket_type, fake_socket = created[0]
        assert sock is fake_socket
        assert family == socket.AF_INET6
        assert socket_type == socket.SOCK_DGRAM

        expected_calls = [
            (socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1),
        ]
        if reuse_address:
            expected_calls.append(
                (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            )
        assert fake_socket.setsockopt_calls == expected_calls
    finally:
        sock.close()


@pytest.mark.parametrize("reuse_address", (False, True))
def test_ipv4_listener_never_sets_v6only(monkeypatch, reuse_address):
    created = _install_recording_socket_factory(monkeypatch)

    sock = udp_listener.create_udp_listener_socket(
        "0.0.0.0",
        reuse_address=reuse_address,
    )
    try:
        assert len(created) == 1
        family, socket_type, fake_socket = created[0]
        assert sock is fake_socket
        assert family == socket.AF_INET
        assert socket_type == socket.SOCK_DGRAM

        expected_calls = []
        if reuse_address:
            expected_calls.append(
                (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            )
        assert fake_socket.setsockopt_calls == expected_calls
    finally:
        sock.close()


def test_ipv6_listener_closes_when_v6only_configuration_fails(monkeypatch):
    _require_ipv6_constants()
    failure = OSError("IPV6_V6ONLY failed")
    created = _install_recording_socket_factory(
        monkeypatch,
        option_failure=failure,
    )

    with pytest.raises(OSError) as exc_info:
        udp_listener.create_udp_listener_socket("::")

    assert exc_info.value is failure
    assert len(created) == 1
    _, _, fake_socket = created[0]
    assert fake_socket.setsockopt_calls == [
        (socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1),
    ]
    assert fake_socket.close_count == 1


def test_ipv4_listener_closes_when_reuseaddr_configuration_fails(monkeypatch):
    failure = OSError("SO_REUSEADDR failed")
    created = _install_recording_socket_factory(
        monkeypatch,
        option_failure=failure,
    )

    with pytest.raises(OSError) as exc_info:
        udp_listener.create_udp_listener_socket(
            "0.0.0.0",
            reuse_address=True,
        )

    assert exc_info.value is failure
    assert len(created) == 1
    _, _, fake_socket = created[0]
    assert fake_socket.setsockopt_calls == [
        (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
    ]
    assert fake_socket.close_count == 1


def test_real_ipv6_listener_reports_v6only_enabled():
    _require_ipv6_loopback()

    with closing(udp_listener.create_udp_listener_socket("::")) as listener:
        assert listener.getsockopt(
            socket.IPPROTO_IPV6,
            socket.IPV6_V6ONLY,
        ) == 1


def _assert_only_expected_listener_receives(
    sender,
    destination,
    listeners,
    expected_listener,
    unexpected_listener,
    payload,
):
    assert sender.sendto(payload, destination) == len(payload)

    readable, _, _ = select.select(
        listeners,
        (),
        (),
        NETWORK_TIMEOUT,
    )
    assert set(readable) == {expected_listener}

    received, _ = expected_listener.recvfrom(8192)
    assert received == payload

    late_readable, _, _ = select.select(
        (unexpected_listener,),
        (),
        (),
        ISOLATION_TIMEOUT,
    )
    assert late_readable == []


@pytest.mark.parametrize(
    "reuse_address",
    (
        pytest.param(True, id="plain-udp"),
        pytest.param(False, id="udpsec"),
    ),
)
def test_ipv4_ipv6_wildcards_bind_same_port_number_and_isolate_traffic(
    reuse_address,
):
    _require_ipv6_loopback()

    with ExitStack() as stack:
        ipv4_listener = stack.enter_context(
            closing(
                udp_listener.create_udp_listener_socket(
                    "0.0.0.0",
                    reuse_address=reuse_address,
                )
            )
        )
        ipv4_listener.bind(("0.0.0.0", 0))
        shared_port = ipv4_listener.getsockname()[1]
        assert shared_port != 0

        ipv6_listener = stack.enter_context(
            closing(
                udp_listener.create_udp_listener_socket(
                    "::",
                    reuse_address=reuse_address,
                )
            )
        )
        ipv6_listener.bind(("::", shared_port))
        assert ipv6_listener.getsockname()[1] == shared_port
        assert ipv6_listener.getsockopt(
            socket.IPPROTO_IPV6,
            socket.IPV6_V6ONLY,
        ) == 1

        ipv4_sender = stack.enter_context(
            closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        )
        ipv4_sender.bind(("127.0.0.1", 0))
        ipv6_sender = stack.enter_context(
            closing(socket.socket(socket.AF_INET6, socket.SOCK_DGRAM))
        )
        ipv6_sender.bind(("::1", 0))

        listeners = (ipv4_listener, ipv6_listener)
        _assert_only_expected_listener_receives(
            ipv4_sender,
            ("127.0.0.1", shared_port),
            listeners,
            ipv4_listener,
            ipv6_listener,
            b"ipv4-only",
        )
        _assert_only_expected_listener_receives(
            ipv6_sender,
            ("::1", shared_port),
            listeners,
            ipv6_listener,
            ipv4_listener,
            b"ipv6-only",
        )
