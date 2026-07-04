import asyncio as real_asyncio
from collections import defaultdict
from pathlib import Path

import yaml

import aismixer
import forwarder as forwarder_module
from assembler import AIVDMAssembler
from core.event import IngressEvent
from core.routing import RoutingTable
from core.runtime_routing import load_optional_routing_table
from dedup import Deduplicator
from forwarder import Forwarder


SENTENCE = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C"
LEGACY_ADDR = ("127.0.0.1", 19000)
TARGET_A_ADDR = ("127.0.0.1", 19001)
TARGET_B_ADDR = ("127.0.0.1", 19002)
SHARED_ADDR = ("127.0.0.1", 19003)


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


def make_event(source_id):
    return IngressEvent(
        kind="udp",
        source_id=source_id,
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key=source_id,
        raw_line=SENTENCE,
    )


def split_targets_forwarders():
    return [
        {"host": LEGACY_ADDR[0], "port": LEGACY_ADDR[1]},
        {"id": "target_a", "host": TARGET_A_ADDR[0], "port": TARGET_A_ADDR[1]},
        {"id": "target_b", "host": TARGET_B_ADDR[0], "port": TARGET_B_ADDR[1]},
    ]


def shared_target_forwarders():
    return [
        {"host": LEGACY_ADDR[0], "port": LEGACY_ADDR[1]},
        {"id": "shared", "host": SHARED_ADDR[0], "port": SHARED_ADDR[1]},
    ]


def split_targets_routing_table():
    return RoutingTable.from_definitions(
        {
            "source_a": {"include": ["udp:source_a"]},
            "source_b": {"include": ["udp:source_b"]},
        },
        [
            {
                "name": "source_a_to_target_a",
                "from_zone": "source_a",
                "to": ["udp:target_a"],
            },
            {
                "name": "source_b_to_target_b",
                "from_zone": "source_b",
                "to": ["udp:target_b"],
            },
        ],
    )


def shared_target_routing_table():
    return RoutingTable.from_definitions(
        {
            "source_a": {"include": ["udp:source_a"]},
            "source_b": {"include": ["udp:source_b"]},
        },
        [
            {
                "name": "source_a_to_shared",
                "from_zone": "source_a",
                "to": ["udp:shared"],
            },
            {
                "name": "source_b_to_shared",
                "from_zone": "source_b",
                "to": ["udp:shared"],
            },
        ],
    )


def datagrams_by_addr(fake_loop):
    by_addr = defaultdict(list)
    for remote_addr, data, _transport in fake_loop.sends:
        by_addr[remote_addr].append(data)
    return by_addr


async def wait_for_datagrams(fake_loop, task, count, timeout=0.5):
    async def _wait():
        while len(fake_loop.sends) < count:
            if task.done():
                task.result()
            await real_asyncio.sleep(0.01)

    await real_asyncio.wait_for(_wait(), timeout=timeout)


async def cancel_task(task):
    task.cancel()
    try:
        await task
    except real_asyncio.CancelledError:
        pass


async def run_routed_events(
    monkeypatch,
    forwarders_config,
    routing_table,
    events,
    expected_datagrams,
):
    fake_loop = _FakeLoop()
    monkeypatch.setattr(
        forwarder_module, "asyncio", _FakeAsyncioModule(fake_loop)
    )
    monkeypatch.setattr(aismixer, "forwarder", Forwarder(forwarders_config))
    monkeypatch.setattr(aismixer, "deduplicator", Deduplicator())
    monkeypatch.setattr(aismixer, "assembler", AIVDMAssembler())
    monkeypatch.setattr(aismixer, "STATION_ID", "test_station")
    monkeypatch.setattr(aismixer, "DEBUG", False)
    monkeypatch.setattr(aismixer, "C_PRESERVE_INGRESS_C", True)
    monkeypatch.setattr(aismixer, "G_PRESERVE_INGRESS_GID", True)
    monkeypatch.setattr(aismixer, "G_ALWAYS_TAG_SINGLE", False)

    queue = real_asyncio.Queue()
    task = real_asyncio.create_task(
        aismixer.forward_loop(queue, routing_table=routing_table)
    )
    try:
        for event in events:
            await queue.put(event)

        if expected_datagrams:
            await wait_for_datagrams(fake_loop, task, expected_datagrams)
        else:
            await real_asyncio.sleep(0.05)
            assert not task.done()

        await real_asyncio.sleep(0.05)
        if task.done():
            task.result()
        return fake_loop
    finally:
        await cancel_task(task)


def test_routed_udp_delivers_same_sentence_to_distinct_targets(monkeypatch):
    fake_loop = real_asyncio.run(
        run_routed_events(
            monkeypatch,
            split_targets_forwarders(),
            split_targets_routing_table(),
            [make_event("udp:source_a"), make_event("udp:source_b")],
            expected_datagrams=2,
        )
    )

    by_addr = datagrams_by_addr(fake_loop)

    assert LEGACY_ADDR not in by_addr
    assert len(by_addr[TARGET_A_ADDR]) == 1
    assert len(by_addr[TARGET_B_ADDR]) == 1
    assert SENTENCE.encode() in by_addr[TARGET_A_ADDR][0]
    assert SENTENCE.encode() in by_addr[TARGET_B_ADDR][0]


def test_routed_udp_deduplicates_same_sentence_for_shared_target(monkeypatch):
    fake_loop = real_asyncio.run(
        run_routed_events(
            monkeypatch,
            shared_target_forwarders(),
            shared_target_routing_table(),
            [make_event("udp:source_a"), make_event("udp:source_b")],
            expected_datagrams=1,
        )
    )

    by_addr = datagrams_by_addr(fake_loop)

    assert LEGACY_ADDR not in by_addr
    assert len(by_addr[SHARED_ADDR]) == 1
    assert SENTENCE.encode() in by_addr[SHARED_ADDR][0]


def test_routed_udp_no_route_sends_no_datagram_and_task_stays_running(monkeypatch):
    fake_loop = real_asyncio.run(
        run_routed_events(
            monkeypatch,
            split_targets_forwarders(),
            split_targets_routing_table(),
            [make_event("udp:no_route")],
            expected_datagrams=0,
        )
    )

    assert fake_loop.sends == []


def test_example_routing_config_is_internally_valid():
    path = Path(__file__).resolve().parents[1] / "examples" / "config-routing.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))

    forwarder = Forwarder(config["forwarders"])
    table = load_optional_routing_table(config, forwarder.target_ids)

    assert forwarder.target_ids == ("udp:aishub", "udp:local_debug")
    assert table.match("udp:balchik_roof").target_ids == (
        "udp:local_debug",
        "udp:aishub",
    )
    assert table.match("udpsec:vitara_mobile").target_ids == ("udp:local_debug",)
