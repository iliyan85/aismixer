import asyncio
import re

import aismixer
from assembler import AIVDMAssembler
from core.event import IngressEvent
from dedup import Deduplicator


SENTENCE = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C"
MULTIPART_FIRST = "!AIVDM,2,1,7,A,payload1,0*00"
MULTIPART_SECOND = "!AIVDM,2,2,7,A,payload2,0*00"


class FakeForwarder:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def make_event(raw_line):
    return IngressEvent(
        kind="udp",
        alias_for_s=None,
        remote_ip="192.0.2.10",
        assembler_key="192.0.2.10:17778",
        raw_line=raw_line,
    )


def leading_tag(message):
    end = message.find("\\", 1)
    return message[1:end]


async def wait_for_sends(fake_forwarder, task, count, timeout=0.5):
    async def _wait():
        while len(fake_forwarder.messages) < count:
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
