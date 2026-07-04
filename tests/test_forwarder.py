import asyncio as real_asyncio

import pytest

import forwarder as forwarder_module
from forwarder import (
    Forwarder,
    ForwarderConfigError,
    UnknownForwarderTargetError,
)


class _FakeTransport:
    def __init__(self, loop, remote_addr):
        self.loop = loop
        self.remote_addr = remote_addr
        self.sent = []

    def sendto(self, data):
        self.sent.append(data)
        self.loop.sends.append((self.remote_addr, data, self))


class _FakeLoop:
    def __init__(self):
        self.created = []
        self.sends = []

    async def create_datagram_endpoint(self, protocol_factory, remote_addr):
        transport = _FakeTransport(self, remote_addr)
        protocol = protocol_factory()
        self.created.append((remote_addr, transport))
        return transport, protocol


class _FakeAsyncioModule:
    DatagramProtocol = real_asyncio.DatagramProtocol

    def __init__(self, loop):
        self._loop = loop

    def get_running_loop(self):
        return self._loop


def _patch_forwarder_loop(monkeypatch):
    loop = _FakeLoop()
    monkeypatch.setattr(forwarder_module, "asyncio", _FakeAsyncioModule(loop))
    return loop


def _targets():
    return [
        {"host": "127.0.0.1", "port": 19000},
        {"id": "aishub", "host": "192.0.2.20", "port": 10110},
        {"id": "local_debug", "host": "127.0.0.1", "port": 19001},
    ]


def test_send_broadcasts_to_legacy_and_named_entries(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder(_targets())

    real_asyncio.run(forwarder.send("message"))

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("127.0.0.1", 19000), b"message"),
        (("192.0.2.20", 10110), b"message"),
        (("127.0.0.1", 19001), b"message"),
    ]


def test_target_ids_returns_only_explicit_ids_in_declaration_order():
    forwarder = Forwarder(_targets())

    assert forwarder.target_ids == ("udp:aishub", "udp:local_debug")
    with pytest.raises(TypeError):
        forwarder.target_ids[0] = "udp:changed"


def test_entries_without_id_remain_valid_for_legacy_broadcast(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder([{"host": "127.0.0.1", "port": 19000}])

    assert forwarder.target_ids == ()

    real_asyncio.run(forwarder.send("legacy"))

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("127.0.0.1", 19000), b"legacy")
    ]


def test_send_to_sends_only_requested_targets(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder(_targets())

    real_asyncio.run(forwarder.send_to(("udp:local_debug",), "targeted"))

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("127.0.0.1", 19001), b"targeted")
    ]


def test_send_to_preserves_requested_order(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder(_targets())

    real_asyncio.run(
        forwarder.send_to(("udp:local_debug", "udp:aishub"), "ordered")
    )

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("127.0.0.1", 19001), b"ordered"),
        (("192.0.2.20", 10110), b"ordered"),
    ]


def test_send_to_deduplicates_repeated_target_ids_by_first_occurrence(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder(_targets())

    real_asyncio.run(
        forwarder.send_to(
            ("udp:local_debug", "udp:aishub", "udp:local_debug"),
            "deduped",
        )
    )

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("127.0.0.1", 19001), b"deduped"),
        (("192.0.2.20", 10110), b"deduped"),
    ]


def test_send_to_rejects_unknown_target_id(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder(_targets())

    with pytest.raises(UnknownForwarderTargetError, match="udp:missing"):
        real_asyncio.run(forwarder.send_to(("udp:missing",), "lost"))

    assert loop.sends == []


def test_duplicate_configured_ids_are_rejected():
    with pytest.raises(ForwarderConfigError, match="udp:archive"):
        Forwarder([
            {"id": "archive", "host": "192.0.2.1", "port": 10001},
            {"id": "archive", "host": "192.0.2.2", "port": 10002},
        ])


def test_original_config_mutation_after_construction_does_not_change_behavior(
    monkeypatch,
):
    loop = _patch_forwarder_loop(monkeypatch)
    config = [
        {"host": "127.0.0.1", "port": 19000},
        {"id": "aishub", "host": "192.0.2.20", "port": 10110},
    ]
    forwarder = Forwarder(config)

    config[0]["host"] = "203.0.113.50"
    config[0]["port"] = 9999
    config[1]["id"] = "changed"
    config[1]["host"] = "203.0.113.51"
    config.append({"id": "new", "host": "203.0.113.52", "port": 9998})

    assert forwarder.target_ids == ("udp:aishub",)

    real_asyncio.run(forwarder.send("unchanged"))
    real_asyncio.run(forwarder.send_to(("udp:aishub",), "named"))

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("127.0.0.1", 19000), b"unchanged"),
        (("192.0.2.20", 10110), b"unchanged"),
        (("192.0.2.20", 10110), b"named"),
    ]


def test_targeted_sends_reuse_transport_cache(monkeypatch):
    loop = _patch_forwarder_loop(monkeypatch)
    forwarder = Forwarder(_targets())

    real_asyncio.run(forwarder.send_to(("udp:aishub",), "one"))
    real_asyncio.run(forwarder.send_to(("udp:aishub",), "two"))

    assert [(addr, data) for addr, data, _ in loop.sends] == [
        (("192.0.2.20", 10110), b"one"),
        (("192.0.2.20", 10110), b"two"),
    ]
    assert [addr for addr, _ in loop.created] == [("192.0.2.20", 10110)]
