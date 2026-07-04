import asyncio
import re

import pytest

import aismixer
from assembler import AIVDMAssembler
from core.event import IngressEvent
from core.routing import RoutingResult, RoutingTable
from dedup import Deduplicator


SENTENCE = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C"
SECOND_SENTENCE = "!AIVDM,1,1,,B,25Muq?002>G?svP00<:O?vN60<0,0*00"
MULTIPART_FIRST = "!AIVDM,2,1,7,A,payload1,0*00"
MULTIPART_SECOND = "!AIVDM,2,2,7,A,payload2,0*00"


class FakeForwarder:
    def __init__(self):
        self.messages = []
        self.targeted_messages = []

    async def send(self, message):
        self.messages.append(message)

    async def send_to(self, target_ids, message):
        self.targeted_messages.append((tuple(target_ids), message))


class _OnePacketLoop:
    def __init__(self, packet):
        self.packet = packet

    async def sock_recvfrom(self, sock, size):
        if self.packet is not None:
            packet = self.packet
            self.packet = None
            return packet
        raise asyncio.CancelledError()


class _FakeAsyncioModule:
    def __init__(self, loop):
        self._loop = loop

    def get_running_loop(self):
        return self._loop


class _FakeQueue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


def make_event(
    raw_line,
    source_id="udp:192.0.2.10",
    alias_for_s=None,
    remote_ip="192.0.2.10",
    assembler_key="192.0.2.10:17778",
):
    return IngressEvent(
        kind="udp",
        source_id=source_id,
        alias_for_s=alias_for_s,
        remote_ip=remote_ip,
        assembler_key=assembler_key,
        raw_line=raw_line,
    )


def leading_tag(message):
    end = message.find("\\", 1)
    return message[1:end]


def make_routing_table(routes=None, zones=None):
    return RoutingTable.from_definitions(
        zones or {
            "source_a": {"include": ["udp:source_a"]},
            "source_b": {"include": ["udp:source_b"]},
        },
        routes or [
            {
                "name": "source_a_to_aishub",
                "from_zone": "source_a",
                "to": ["udp:aishub"],
            }
        ],
    )


class RecordingRoutingTable:
    def __init__(self, target_ids):
        self.target_ids = tuple(target_ids)
        self.source_ids = []

    def match(self, source_id):
        self.source_ids.append(source_id)
        return RoutingResult(("recorded",), self.target_ids)


def test_handle_socket_creates_ingress_event_with_udp_source_id(monkeypatch):
    queue = _FakeQueue()
    fake_loop = _OnePacketLoop(
        (SENTENCE.encode(), ("192.0.2.10", 17778))
    )

    monkeypatch.setattr(aismixer, "asyncio", _FakeAsyncioModule(fake_loop))
    monkeypatch.setattr(aismixer, "DEBUG", False)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            aismixer.handle_socket(
                object(),
                queue,
                fixed_alias="balchik_roof",
                alias_map={"192.0.2.10": "dock_gate"},
            )
        )

    assert len(queue.items) == 1
    event = queue.items[0]
    assert event.kind == "udp"
    assert event.source_id == "udp:balchik_roof"
    assert event.alias_for_s == "balchik_roof"
    assert event.remote_ip == "192.0.2.10"
    assert event.assembler_key == "192.0.2.10:17778"
    assert event.raw_line == SENTENCE


async def wait_for_sends(fake_forwarder, task, count, timeout=0.5):
    async def _wait():
        while len(fake_forwarder.messages) < count:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=timeout)


async def wait_for_forwarder_activity(
    fake_forwarder,
    task,
    broadcast_count=0,
    targeted_count=0,
    timeout=0.5,
):
    async def _wait():
        while (
            len(fake_forwarder.messages) < broadcast_count
            or len(fake_forwarder.targeted_messages) < targeted_count
        ):
            if task.done():
                task.result()
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=timeout)


async def cancel_task(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def run_forward_loop_events(monkeypatch, events, expected_sends):
    fake_forwarder = FakeForwarder()
    monkeypatch.setattr(aismixer, "forwarder", fake_forwarder)
    monkeypatch.setattr(aismixer, "assembler", AIVDMAssembler())
    monkeypatch.setattr(aismixer, "deduplicator", Deduplicator())
    monkeypatch.setattr(aismixer, "STATION_ID", "test_station")
    monkeypatch.setattr(aismixer, "DEBUG", False)
    monkeypatch.setattr(aismixer, "C_PRESERVE_INGRESS_C", True)
    monkeypatch.setattr(aismixer, "G_PRESERVE_INGRESS_GID", True)
    monkeypatch.setattr(aismixer, "G_ALWAYS_TAG_SINGLE", False)

    queue = asyncio.Queue()
    task = asyncio.create_task(aismixer.forward_loop(queue))
    try:
        for event in events:
            await queue.put(event)

        if expected_sends:
            await wait_for_sends(fake_forwarder, task, expected_sends)

        await asyncio.sleep(0.05)
        if task.done():
            task.result()
        return list(fake_forwarder.messages)
    finally:
        await cancel_task(task)


async def run_forward_loop_capture(
    monkeypatch,
    events,
    expected_broadcast_sends=0,
    expected_targeted_sends=0,
    routing_table=None,
    station_id="test_station",
):
    fake_forwarder = FakeForwarder()
    monkeypatch.setattr(aismixer, "forwarder", fake_forwarder)
    monkeypatch.setattr(aismixer, "assembler", AIVDMAssembler())
    monkeypatch.setattr(aismixer, "deduplicator", Deduplicator())
    monkeypatch.setattr(aismixer, "STATION_ID", station_id)
    monkeypatch.setattr(aismixer, "DEBUG", False)
    monkeypatch.setattr(aismixer, "C_PRESERVE_INGRESS_C", True)
    monkeypatch.setattr(aismixer, "G_PRESERVE_INGRESS_GID", True)
    monkeypatch.setattr(aismixer, "G_ALWAYS_TAG_SINGLE", False)

    queue = asyncio.Queue()
    task = asyncio.create_task(
        aismixer.forward_loop(queue, routing_table=routing_table)
    )
    try:
        for event in events:
            await queue.put(event)

        if expected_broadcast_sends or expected_targeted_sends:
            await wait_for_forwarder_activity(
                fake_forwarder,
                task,
                broadcast_count=expected_broadcast_sends,
                targeted_count=expected_targeted_sends,
            )

        await asyncio.sleep(0.05)
        if task.done():
            task.result()
        return fake_forwarder
    finally:
        await cancel_task(task)


async def run_multipart_forward_loop(monkeypatch):
    fake_forwarder = FakeForwarder()
    monkeypatch.setattr(aismixer, "forwarder", fake_forwarder)
    monkeypatch.setattr(aismixer, "assembler", AIVDMAssembler())
    monkeypatch.setattr(aismixer, "deduplicator", Deduplicator())
    monkeypatch.setattr(aismixer, "STATION_ID", "test_station")
    monkeypatch.setattr(aismixer, "DEBUG", False)
    monkeypatch.setattr(aismixer, "C_PRESERVE_INGRESS_C", True)
    monkeypatch.setattr(aismixer, "G_PRESERVE_INGRESS_GID", True)
    monkeypatch.setattr(aismixer, "G_ALWAYS_TAG_SINGLE", False)

    queue = asyncio.Queue()
    task = asyncio.create_task(aismixer.forward_loop(queue))
    try:
        await queue.put(make_event(MULTIPART_FIRST))
        await asyncio.sleep(0.05)
        if task.done():
            task.result()
        assert fake_forwarder.messages == []

        await queue.put(make_event(MULTIPART_SECOND))
        await wait_for_sends(fake_forwarder, task, 2)
        return list(fake_forwarder.messages)
    finally:
        await cancel_task(task)


def test_forward_loop_forwards_single_plain_aivdm(monkeypatch):
    messages = asyncio.run(
        run_forward_loop_events(monkeypatch, [make_event(SENTENCE)], expected_sends=1)
    )

    assert len(messages) == 1
    assert SENTENCE in messages[0]
    assert messages[0].endswith("\r\n")
    assert messages[0].startswith("\\c:")
    assert ",s:test_station" in messages[0]


def test_forward_loop_preserves_ingress_c_but_station_id_wins_for_s(monkeypatch):
    raw_line = "\\c:1234567890,s:ingress_station*00\\" + SENTENCE

    messages = asyncio.run(
        run_forward_loop_events(monkeypatch, [make_event(raw_line)], expected_sends=1)
    )

    tag = leading_tag(messages[0])
    assert tag.startswith("c:1234567890,s:test_station*")
    assert "ingress_station" not in tag
    assert SENTENCE in messages[0]


def test_forward_loop_ignores_non_ais_input_without_crashing(monkeypatch):
    messages = asyncio.run(
        run_forward_loop_events(monkeypatch, [make_event("not ais input")], expected_sends=0)
    )

    assert messages == []


def test_forward_loop_deduplicates_duplicate_plain_aivdm(monkeypatch):
    messages = asyncio.run(
        run_forward_loop_events(
            monkeypatch,
            [make_event(SENTENCE), make_event(SENTENCE)],
            expected_sends=1,
        )
    )

    assert len(messages) == 1
    assert SENTENCE in messages[0]


def test_forward_loop_buffers_multipart_until_second_fragment(monkeypatch):
    messages = asyncio.run(run_multipart_forward_loop(monkeypatch))

    assert len(messages) == 2
    assert MULTIPART_FIRST in messages[0]
    assert MULTIPART_SECOND in messages[1]

    first_tag = leading_tag(messages[0])
    second_tag = leading_tag(messages[1])

    assert re.fullmatch(r"c:\d+,s:test_station,g:1-2-\d+\*[0-9A-F]{2}", first_tag)
    assert re.fullmatch(r"g:2-2-\d+\*[0-9A-F]{2}", second_tag)
    assert "c:" not in second_tag
    assert "s:" not in second_tag


def test_forward_loop_legacy_mode_calls_send_not_send_to(monkeypatch):
    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [make_event(SENTENCE)],
            expected_broadcast_sends=1,
        )
    )

    assert len(fake_forwarder.messages) == 1
    assert fake_forwarder.targeted_messages == []


def test_forward_loop_legacy_mode_keeps_global_deduplication(monkeypatch):
    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [make_event(SENTENCE), make_event(SENTENCE)],
            expected_broadcast_sends=1,
        )
    )

    assert len(fake_forwarder.messages) == 1
    assert fake_forwarder.targeted_messages == []


def test_forward_loop_routing_mode_matches_event_source_id(monkeypatch):
    routing_table = RecordingRoutingTable(("udp:aishub",))
    event = make_event(SENTENCE, source_id="udp:source_a")

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [event],
            expected_targeted_sends=1,
            routing_table=routing_table,
        )
    )

    assert routing_table.source_ids == ["udp:source_a"]
    assert fake_forwarder.messages == []


def test_forward_loop_routing_mode_sends_to_matched_targets(monkeypatch):
    routing_table = make_routing_table()
    event = make_event(SENTENCE, source_id="udp:source_a")

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [event],
            expected_targeted_sends=1,
            routing_table=routing_table,
        )
    )

    assert fake_forwarder.messages == []
    assert len(fake_forwarder.targeted_messages) == 1
    target_ids, message = fake_forwarder.targeted_messages[0]
    assert target_ids == ("udp:aishub",)
    assert SENTENCE in message


def test_forward_loop_routing_mode_no_matching_route_produces_no_output(monkeypatch):
    routing_table = make_routing_table()
    event = make_event(SENTENCE, source_id="udp:unmatched")

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [event],
            routing_table=routing_table,
        )
    )

    assert fake_forwarder.messages == []
    assert fake_forwarder.targeted_messages == []


def test_forward_loop_routing_mode_suppresses_duplicate_for_same_target(monkeypatch):
    routing_table = make_routing_table()
    events = [
        make_event(SENTENCE, source_id="udp:source_a"),
        make_event(SENTENCE, source_id="udp:source_a"),
    ]

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            events,
            expected_targeted_sends=1,
            routing_table=routing_table,
        )
    )

    assert len(fake_forwarder.targeted_messages) == 1
    assert fake_forwarder.targeted_messages[0][0] == ("udp:aishub",)


def test_forward_loop_routing_mode_allows_same_sentence_to_two_targets(monkeypatch):
    routing_table = make_routing_table(
        routes=[
            {
                "name": "source_a_to_two_targets",
                "from_zone": "source_a",
                "to": ["udp:aishub", "udp:local_debug"],
            }
        ]
    )
    event = make_event(SENTENCE, source_id="udp:source_a")

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [event],
            expected_targeted_sends=1,
            routing_table=routing_table,
        )
    )

    assert fake_forwarder.targeted_messages[0][0] == (
        "udp:aishub",
        "udp:local_debug",
    )


def test_forward_loop_routing_mode_dedups_two_sources_to_same_target(monkeypatch):
    routing_table = make_routing_table(
        routes=[
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
        ]
    )
    events = [
        make_event(SENTENCE, source_id="udp:source_a", assembler_key="a:1"),
        make_event(SENTENCE, source_id="udp:source_b", assembler_key="b:1"),
    ]

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            events,
            expected_targeted_sends=1,
            routing_table=routing_table,
        )
    )

    assert len(fake_forwarder.targeted_messages) == 1
    assert fake_forwarder.targeted_messages[0][0] == ("udp:shared",)


def test_forward_loop_routing_mode_keeps_two_sources_to_different_targets(monkeypatch):
    routing_table = make_routing_table(
        routes=[
            {
                "name": "source_a_to_aishub",
                "from_zone": "source_a",
                "to": ["udp:aishub"],
            },
            {
                "name": "source_b_to_debug",
                "from_zone": "source_b",
                "to": ["udp:local_debug"],
            },
        ]
    )
    events = [
        make_event(SENTENCE, source_id="udp:source_a", assembler_key="a:1"),
        make_event(SENTENCE, source_id="udp:source_b", assembler_key="b:1"),
    ]

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            events,
            expected_targeted_sends=2,
            routing_table=routing_table,
        )
    )

    assert [target_ids for target_ids, _ in fake_forwarder.targeted_messages] == [
        ("udp:aishub",),
        ("udp:local_debug",),
    ]


def test_forward_loop_routing_mode_overlapping_routes_do_not_duplicate_target(
    monkeypatch,
):
    routing_table = make_routing_table(
        routes=[
            {
                "name": "source_a_to_shared_first",
                "from_zone": "source_a",
                "to": ["udp:shared"],
            },
            {
                "name": "source_a_to_shared_second",
                "from_zone": "source_a",
                "to": ["udp:shared"],
            },
        ]
    )
    event = make_event(SENTENCE, source_id="udp:source_a")

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [event],
            expected_targeted_sends=1,
            routing_table=routing_table,
        )
    )

    assert fake_forwarder.targeted_messages[0][0] == ("udp:shared",)


def test_forward_loop_routing_mode_preserves_multipart_and_tag_behavior(monkeypatch):
    routing_table = make_routing_table()
    events = [
        make_event(MULTIPART_FIRST, source_id="udp:source_a"),
        make_event(MULTIPART_SECOND, source_id="udp:source_a"),
    ]

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            events,
            expected_targeted_sends=2,
            routing_table=routing_table,
        )
    )

    messages = [message for _, message in fake_forwarder.targeted_messages]
    assert len(messages) == 2
    assert MULTIPART_FIRST in messages[0]
    assert MULTIPART_SECOND in messages[1]

    first_tag = leading_tag(messages[0])
    second_tag = leading_tag(messages[1])

    assert re.fullmatch(r"c:\d+,s:test_station,g:1-2-\d+\*[0-9A-F]{2}", first_tag)
    assert re.fullmatch(r"g:2-2-\d+\*[0-9A-F]{2}", second_tag)
    assert "c:" not in second_tag
    assert "s:" not in second_tag


def test_forward_loop_routing_matches_once_for_multi_sentence_event(monkeypatch):
    routing_table = RecordingRoutingTable(("udp:aishub",))
    raw_line = f"{SENTENCE}\n{SECOND_SENTENCE}"

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            [make_event(raw_line, source_id="udp:source_a")],
            expected_targeted_sends=2,
            routing_table=routing_table,
        )
    )

    assert routing_table.source_ids == ["udp:source_a"]
    assert fake_forwarder.messages == []
    assert len(fake_forwarder.targeted_messages) == 2
    assert [target_ids for target_ids, _ in fake_forwarder.targeted_messages] == [
        ("udp:aishub",),
        ("udp:aishub",),
    ]
    assert SENTENCE in fake_forwarder.targeted_messages[0][1]
    assert SECOND_SENTENCE in fake_forwarder.targeted_messages[1][1]


def test_forward_loop_routing_cleans_multipart_context_when_no_route_matches(
    monkeypatch,
):
    routing_table = make_routing_table()
    assembler_key = "shared-receiver"
    group_id = "424242"
    first_unrouted = (
        f"\\s:stale_source,g:1-2-{group_id}*00\\{MULTIPART_FIRST}"
    )
    second_unrouted = f"\\g:2-2-{group_id}*00\\{MULTIPART_SECOND}"
    first_routed = f"\\g:1-2-{group_id}*00\\{MULTIPART_FIRST}"
    second_routed = f"\\g:2-2-{group_id}*00\\{MULTIPART_SECOND}"
    events = [
        make_event(
            first_unrouted,
            source_id="udp:unmatched",
            assembler_key=assembler_key,
        ),
        make_event(
            second_unrouted,
            source_id="udp:unmatched",
            assembler_key=assembler_key,
        ),
        make_event(
            first_routed,
            source_id="udp:source_a",
            assembler_key=assembler_key,
        ),
        make_event(
            second_routed,
            source_id="udp:source_a",
            assembler_key=assembler_key,
        ),
    ]

    fake_forwarder = asyncio.run(
        run_forward_loop_capture(
            monkeypatch,
            events,
            expected_targeted_sends=2,
            routing_table=routing_table,
            station_id="",
        )
    )

    assert fake_forwarder.messages == []
    assert len(fake_forwarder.targeted_messages) == 2
    first_message = fake_forwarder.targeted_messages[0][1]
    first_tag = leading_tag(first_message)
    assert "stale_source" not in first_tag
    assert ",s:192_0_2_10," in first_tag
