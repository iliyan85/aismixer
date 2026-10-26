"""Independent A1-S4-A local-intent trace oracle (standard library only).

CLASSA-ACCESS-TRACE-v1 is an explicitly synthetic host-test protocol. The
reference below transcribes CLASSA-FIRST-FRAME-v1, LCP-P01/P02/P05/P08 and
the accepted semantic Philox packing; it imports no production implementation,
encoder, layout table or expected-output file. No row denotes radio success.

Pipe-separated ASCII schema; '-' is an absent optional integer/list, comma
separates ascending ledger IDs. All times are signed SimulationInstant units
(9,830,400/second); all slot indices are true signed absolute UTC slots.
The frozen fixture's display_base is only a human label origin.

ACCESS: schema ID.
SCENE: name, seed(u64), owner vessel/equipment/radio IDs, initial ON time,
       anchor POSIX seconds/subsecond units, rate numerator/denominator,
       display_base, synthetic fixture kind, longitude/latitude AIS units.
POWER: scene, time, generation, ON/OFF/INITIAL_OFF.
PRESSURE: scene, generation, channel, first index, block length, retention.
DRAW: scene, time, ledger ID, generation, purpose, episode, draw index,
      inclusive minimum/maximum, value, accepted u32 word, rejections,
      FNV64 input digest, typed outcome.
PROPOSAL: scene, time, generation, proposal ID, channel, slot, slot start,
          ordinal, NSS, status, knowledge revision, FNV64 own inputs,
          ledger IDs, typed reason. Always future, never a due payload.
INTENT: scene, time, owner vessel/equipment/radio IDs, generation, decision
        ID, proposal ID, channel, slot, slot start, NSS, actual first NTS,
        ordinal, next slot, raw19, keep, length, status, Type3 flavor,
        knowledge revision, FNV64 own inputs, ledger IDs, typed reason.
STEP: scene, time, generation, state, blocked reason, due/proposal counts.
PLAN: scene, time, generation, channel, NSS, first NTS, next due, done, phase.
WITHDRAW: scene, time, generation, channel, slot, typed reason.
END: scene, state, due count, separate AIS1/AIS2 zero counts, ledger count.
"""

import argparse
from dataclasses import dataclass, field
import hashlib
import os
import subprocess


SECOND = 9_830_400
SLOT = 262_144
FRAME = 589_824_000
FRAME_SLOTS = 2250
MASK32 = (1 << 32) - 1
MASK64 = (1 << 64) - 1


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def nullable(value):
    return None if value == "-" else int(value)


def normalized(value):
    return value.replace("_", "").lower()


def fnv(text):
    result = 14695981039346656037
    for byte in text.encode("ascii"):
        result = ((result ^ byte) * 1099511628211) & MASK64
    return result


def philox(counter, key):
    """Direct ten-round integer definition, checked against a literal KAT."""
    a, b, c, d = counter
    x, y = key
    for _ in range(10):
        product_a = 0xD2511F53 * a
        product_c = 0xCD9E8D57 * c
        a, b, c, d = (
            ((product_c >> 32) ^ b ^ x) & MASK32,
            product_c & MASK32,
            ((product_a >> 32) ^ d ^ y) & MASK32,
            product_a & MASK32,
        )
        x = (x + 0x9E3779B9) & MASK32
        y = (y + 0xBB67AE85) & MASK32
    return a, b, c, d


def reference_draw(seed, equipment, generation, purpose, episode, draw_index,
                   minimum, maximum):
    require(0 < equipment <= MASK32, "draw equipment domain")
    require(1 <= generation <= 0xFFFFFF, "draw generation domain")
    require(purpose in (101, 102, 103), "entry must not draw generic RATDMA/reuse")
    require(0 <= episode <= MASK32 and 0 <= draw_index <= 0xFFFF, "draw key width")
    span = maximum - minimum + 1
    require(1 <= span <= 1 << 32, "draw inclusive span")
    limit = ((1 << 32) // span) * span
    for rejection in range(64):
        words = philox(
            (equipment, episode, generation | purpose << 24,
             draw_index | rejection << 16 | 1 << 24),
            (seed & MASK32, (seed >> 32) & MASK32),
        )
        if words[0] < limit:
            return minimum + words[0] % span, words[0], rejection
    raise AssertionError("reference finite Philox proposal budget exhausted")


def raw_itdma(increment):
    require(0 <= increment <= 16383, "effective ITDMA increment domain")
    # One physical slot. Extended code 5 adds 8192; ordinary code is zero.
    wire, code = (increment, 0) if increment < 8192 else (increment - 8192, 5)
    return wire << 4 | code << 1 | 1


@dataclass
class ChannelModel:
    nss: int | None = None
    first: int | None = None
    next_due: int | None = None
    done: int = 0
    phase: str = "idle"
    prepared: int | None = None
    first_refs: tuple = ()
    prepared_refs: tuple = ()


@dataclass
class Scene:
    name: str
    seed: int
    owner: tuple
    initial_on: int
    anchor_seconds: int
    anchor_phase: int
    rate_numerator: int
    rate_denominator: int
    display_base: int
    fixture: str
    longitude: int
    latitude: int
    generation: int = 0
    on: bool = False
    on_at: int | None = None
    last_at: int | None = None
    state: str = "off"
    channels: dict = field(default_factory=lambda: {
        "AIS1": ChannelModel(), "AIS2": ChannelModel()})
    reservations: list = field(default_factory=list)
    draws: dict = field(default_factory=dict)
    watermarks: dict = field(default_factory=dict)
    waiting_reservation: tuple | None = None
    entry_sequence: int | None = None
    proposals: dict = field(default_factory=dict)
    consumed_proposals: set = field(default_factory=set)
    withdrawal_expected: set = field(default_factory=set)
    due: list = field(default_factory=list)
    decision_ids: set = field(default_factory=set)
    step_due: list = field(default_factory=list)
    step_proposals: list = field(default_factory=list)
    power_events: list = field(default_factory=list)
    ended: bool = False

    @property
    def rate(self):
        if self.rate_denominator == 1 and self.rate_numerator in (6, 10, 18, 30):
            return self.rate_numerator
        return None

    @property
    def ni(self):
        require(self.rate is not None, "unsupported rate has no nominal interval")
        return FRAME_SLOTS // self.rate

    def at(self, time):
        require(self.last_at is None or time >= self.last_at, "trace time reversed")
        self.last_at = time

    def index(self, time):
        absolute_units = self.anchor_seconds * SECOND + self.anchor_phase + time
        return absolute_units // SLOT

    def start(self, index):
        return index * SLOT - self.anchor_seconds * SECOND - self.anchor_phase

    def fingerprint(self, at):
        return fnv(f"{self.longitude}|{self.latitude}|{at}|{at}|{at}|{at}")

    def occupied(self, channel, index):
        for selected, first, length, retained in self.reservations:
            if selected != channel:
                continue
            for repeat in range(retained + 1):
                if first + repeat * FRAME_SLOTS <= index < first + repeat * FRAME_SLOTS + length:
                    return True
        return False

    def pool(self, channel, nominal, now):
        other = "AIS2" if channel == "AIS1" else "AIS1"
        half = self.ni // 10
        return [index for index in range(nominal - half, nominal + half + 1)
                if index > self.index(now)
                and not self.occupied(channel, index)
                and not any(self.occupied(other, index + delta) for delta in (-1, 0, 1))]

    def selection(self, now):
        # The initial pair is selected A, then B. Later choices prepare the
        # successor of exactly the one channel due at this instant.
        for name in ("AIS1", "AIS2"):
            channel = self.channels[name]
            if channel.nss is not None and channel.first is None:
                return name, channel.nss, 0
        due = [(name, channel) for name, channel in self.channels.items()
               if channel.next_due is not None and self.start(channel.next_due) == now]
        require(len(due) == 1, "candidate draw without one due carrying channel")
        name, channel = due[0]
        require(channel.done < self.rate // 2 - 1, "terminal successor must reuse kept first slot")
        return name, channel.nss + (channel.done + 1) * 2 * self.ni, channel.done + 1


def canonical_pool(channel, nominal, ni, candidates):
    result = f"{channel}|L1|NS{nominal}|NI{ni}"
    return result + "".join(f"|{index}:0:free1:free1:-" for index in candidates)


def verify_required_fixtures(scenes):
    """Literal mathematical witnesses, not output copied from the executable.

    Frozen adjudication F1/F2 labels use minute origin 31666666 and a POSIX
    anchor of 1900000000 seconds (40-second minute phase). The brief's zero
    anchor restatement is arithmetically inconsistent with those labels.
    ZERO_ANCHOR separately tests the corrected literal-zero coordinate system.
    """
    base = 71_249_998_500
    expected = {
        "F1": (1062, 7 * SECOND, 1900000000, 0, 6, 1, base, "UNIT_FIXTURE_OWN_PRESSURE"),
        "F2": (939, 10 * SECOND, 1900000000, 0, 6, 1, base, "UNIT_FIXTURE_OWN_PRESSURE"),
        "ZERO_ANCHOR": (1062, 7 * SECOND, 0, 0, 6, 1, 0, "UNIT_FIXTURE_OWN_PRESSURE"),
        "NEGATIVE_ANCHOR": (42, 7 * SECOND, -59, 123, 6, 1, 0, "EMPTY_LOCAL_MAPS"),
        "F3": (42, 7 * SECOND, 1900000000, 0, 1, 3, base, "BLOCKED_LOW_RATE"),
    }
    for rate in (6, 10, 18, 30):
        expected[f"EMPTY_R{rate}"] = (42, 0, 0, 0, rate, 1, 0, "EMPTY_LOCAL_MAPS")
    for mode in range(5):
        expected[f"F4_{mode}"] = (1062, 7 * SECOND, 1900000000, 0, 6, 1, base, "UNIT_FIXTURE_LIFECYCLE")
    require(set(scenes) == set(expected), "required F1/F2/F3/F4/rate/anchor scenes missing or renamed")
    for name, profile in expected.items():
        scene = scenes[name]
        actual = (scene.seed, scene.initial_on, scene.anchor_seconds, scene.anchor_phase,
                  scene.rate_numerator, scene.rate_denominator, scene.display_base, scene.fixture)
        require(actual == profile, f"{name} literal fixture input metadata")
        require(scene.owner == (11, 21, 31) and (scene.longitude, scene.latitude) == (16_200_000, 25_800_000),
                f"{name} synthetic fixture identity/location")
        require(scene.power_events and scene.power_events[0] == (scene.initial_on, 1, "on"),
                f"{name} actual ON must match declared initial ON")
        if name.startswith("F4_"):
            mode = int(name[-1])
            off = (66 * SECOND, 67 * SECOND, 69 * SECOND, 697_827_328, 746_979_328)[mode]
            require(scene.power_events == [(7 * SECOND, 1, "on"), (off, 1, "off")],
                    f"{name} literal OFF boundary")
            expected_due = [(1, "AIS1", base + 4162, 0, 12001)] if mode == 4 else []
            require(scene.state == "off" and scene.due == expected_due, f"{name} OFF dominates due work")
            if mode < 2:
                require(not scene.draws, f"{name} OFF before entry must consume zero draws")
        elif name == "F3":
            require(scene.state == "blocked" and not scene.due and not scene.draws,
                    "F3 low rate must not invent entry, Type1 or terminal zero")
        else:
            require(scene.state == "continuousmarker" and len(scene.due) == scene.rate + 2,
                    f"{name} complete two-channel first frame")

    for name, origin, first_a, first_b in (("F1", base, 4162, 4537),
                                          ("F2", base, 4499, 4874),
                                          ("ZERO_ANCHOR", 0, 2662, 3037)):
        scene = scenes[name]
        for channel, first in (("AIS1", first_a), ("AIS2", first_b)):
            rows = [row for row in scene.due if row[1] == channel]
            literal = [(1, channel, origin + first + ordinal * 750, ordinal,
                        1 if ordinal == 3 else 12001) for ordinal in range(4)]
            require(rows == literal, f"{name}/{channel} literal per-first-NTS chain and terminal word")
    require(scenes["F1"].start(base + 4162) == 697_827_328 and
            scenes["F1"].start(base + 4537) == 796_131_328 and
            scenes["F1"].start(base + 6412) == 1_287_651_328 and
            scenes["F1"].start(base + 6787) == 1_385_955_328,
            "F1 literal first/terminal instants")
    for rate in (6, 10, 18, 30):
        scene = scenes[f"EMPTY_R{rate}"]
        gap = 2 * FRAME_SLOTS // rate
        require(any(row[4] != raw_itdma(gap) for row in scene.due if row[4] != 1),
                f"EMPTY_R{rate} must exercise actual non-nominal increments")


def verify(data):
    require(b"\x00" not in data, "NUL in trace protocol")
    rows = data.decode("ascii").splitlines()
    require(rows and rows[0] == "ACCESS|CLASSA-ACCESS-TRACE-v1", "trace schema/provenance")
    require(philox((0, 0, 0, 0), (0, 0)) ==
            (0x6627E8D5, 0xE169C58D, 0xBC57AC4C, 0x9B00DBD8), "literal Philox KAT")
    scenes = {}
    counts = {"rows": 0, "draws": 0, "intents": 0, "zeros": 0, "plans": 0}
    for line in rows[1:]:
        columns = line.split("|")
        kind = columns[0]
        counts["rows"] += 1
        if kind == "SCENE":
            require(len(columns) == 15, "SCENE shape")
            _, name, seed, vessel, equipment, radio, on, seconds, phase, numerator, denominator, base, fixture, lon, lat = columns
            require(name not in scenes, "duplicate scene")
            scene = Scene(name, int(seed), (int(vessel), int(equipment), int(radio)),
                          int(on), int(seconds), int(phase), int(numerator), int(denominator),
                          int(base), fixture, int(lon), int(lat))
            require(all(value > 0 for value in scene.owner), "synthetic owner IDs")
            require(0 <= scene.anchor_phase < SECOND and scene.rate_denominator > 0, "scene anchor/rate")
            scenes[name] = scene
            continue
        require(len(columns) >= 2 and columns[1] in scenes, "row references unknown scene")
        scene = scenes[columns[1]]
        require(not scene.ended, "row after END")
        if kind == "POWER":
            require(len(columns) == 5, "POWER shape")
            _, _, at, generation, power = columns
            at, generation = int(at), int(generation)
            scene.at(at)
            scene.power_events.append((at, generation, normalized(power)))
            if normalized(power) == "on":
                require(not scene.on and generation > scene.generation, "power ON epoch")
                scene.on, scene.on_at, scene.generation = True, at, generation
                scene.state = "observe60seconds"
                scene.channels = {name: ChannelModel() for name in ("AIS1", "AIS2")}
                scene.reservations.clear()
                scene.watermarks.clear()
            elif normalized(power) in ("off", "initialoff"):
                require(generation >= scene.generation, "power OFF stale generation")
                scene.withdrawal_expected.update(
                    (scene.generation, channel, item.next_due)
                    for channel, item in scene.channels.items() if item.next_due is not None)
                scene.on, scene.generation, scene.state = False, generation, "off"
                scene.channels = {name: ChannelModel() for name in ("AIS1", "AIS2")}
                scene.reservations.clear()
            else:
                raise AssertionError("unknown power kind")
            scene.waiting_reservation = None
        elif kind == "PRESSURE":
            require(len(columns) == 7, "PRESSURE shape")
            _, _, generation, channel, first, length, retained = columns
            require(scene.on and int(generation) == scene.generation, "pressure owner epoch")
            require(channel in scene.channels and 1 <= int(length) <= 5 and int(retained) == 0,
                    "declared UNIT_FIXTURE pressure shape")
            require(scene.fixture in ("UNIT_FIXTURE_OWN_PRESSURE", "UNIT_FIXTURE_LIFECYCLE"),
                    "empty-map scene contains pressure")
            first, length = int(first), int(length)
            require(first // FRAME_SLOTS == (first + length - 1) // FRAME_SLOTS, "pressure crosses frame")
            scene.reservations.append((channel, first, length, int(retained)))
        elif kind == "DRAW":
            require(len(columns) == 15, "DRAW shape")
            _, _, at, sequence, generation, purpose, episode, draw_index, low, high, value, word, rejections, digest, outcome = columns
            at, sequence, generation, purpose, episode, draw_index, low, high, value, word, rejections, digest = map(
                int, (at, sequence, generation, purpose, episode, draw_index, low, high, value, word, rejections, digest))
            scene.at(at)
            require(scene.on and generation == scene.generation and at >= scene.on_at + FRAME,
                    "draw before own observation completion or in wrong epoch")
            require(sequence == len(scene.draws) + 1, "draw ledger identity missing/reused/reordered")
            require(draw_index == 0 and episode == scene.watermarks.get(purpose, -1) + 1,
                    "per-purpose semantic episode counter/replay")
            expected = reference_draw(scene.seed, scene.owner[1], generation, purpose, episode, draw_index, low, high)
            require((value, word, rejections) == expected, "independent semantic Philox word/value/rejection")
            if purpose == 101:
                require((low, high, digest, outcome) == (1, scene.ni, 0, "entry_offset_draw_only"), "entry NSS draw domain")
                nss = scene.index(at) + value
                scene.channels["AIS1"].nss = nss
                scene.channels["AIS2"].nss = nss + scene.ni
                scene.entry_sequence = sequence
            elif purpose == 102:
                name, nominal, ordinal = scene.selection(at)
                candidates = scene.pool(name, nominal, at)
                require(candidates and (low, high) == (0, len(candidates) - 1), "independent future SI candidate range")
                require(digest == fnv(canonical_pool(name, nominal, scene.ni, candidates)), "independent candidate digest")
                selected = candidates[value]
                require(outcome == f"candidate_choice:{selected}", "candidate index not from canonical pool")
                require(scene.waiting_reservation is None, "candidate choice before previous reservation timeout")
                scene.waiting_reservation = name, selected, ordinal, sequence
            elif purpose == 103:
                require((low, high, digest, outcome) == (3, 7, 0, "continuous_timeout"), "timeout draw domain")
                require(scene.waiting_reservation is not None, "timeout without a newly selected slot")
                name, selected, ordinal, choice_sequence = scene.waiting_reservation
                scene.reservations.append((name, selected, 1, value - 1))
                channel = scene.channels[name]
                if ordinal == 0:
                    channel.first, channel.next_due, channel.phase = selected, selected, "waitingfirst"
                    channel.first_refs = (scene.entry_sequence, choice_sequence, sequence)
                else:
                    channel.prepared = selected
                    channel.prepared_refs = (choice_sequence, sequence)
                scene.waiting_reservation = None
            scene.watermarks[purpose] = episode
            scene.draws[sequence] = generation, purpose, value
            counts["draws"] += 1
        elif kind == "PROPOSAL":
            require(len(columns) == 15, "PROPOSAL shape")
            _, _, at, generation, proposal, channel, slot, start, ordinal, nss, status, revision, fingerprint, ledgers, reason = columns
            at, generation, proposal, slot, start, ordinal, nss, revision, fingerprint = map(
                int, (at, generation, proposal, slot, start, ordinal, nss, revision, fingerprint))
            scene.at(at)
            require(scene.on and generation == scene.generation and channel in scene.channels, "proposal own epoch/channel")
            require(proposal > 0 and proposal not in scene.proposals and revision > 0 and reason, "proposal identity/provenance")
            require(start == scene.start(slot) and start > at and normalized(status) == "proposed", "future proposal is not due")
            expected = scene.channels[channel]
            require(nss == expected.nss and ordinal in (expected.done, expected.done + 1), "proposal nominal/ordinal")
            require(slot in (expected.next_due, expected.prepared, None if expected.first is None else expected.first + FRAME_SLOTS),
                    "proposal slot not selected or retained")
            require(fingerprint == scene.fingerprint(at), "proposal own input fingerprint")
            refs = [] if ledgers == "-" else list(map(int, ledgers.split(",")))
            require(all(ref in scene.draws and scene.draws[ref][0] == generation for ref in refs), "proposal ledger references")
            if ordinal == 0:
                require(slot == expected.first, "initial proposal must be selected first NTS")
                expected_refs = expected.first_refs
            elif ordinal == scene.rate // 2:
                require(slot == expected.first + FRAME_SLOTS, "terminal proposal must reuse kept first slot")
                expected_refs = expected.first_refs
            else:
                require(slot == expected.prepared and ordinal == expected.done + 1,
                        "successor proposal must be newly selected ordinal")
                expected_refs = expected.prepared_refs
            require(tuple(refs) == expected_refs and refs, "proposal exact draw lineage")
            scene.proposals[proposal] = generation, channel, slot, ordinal, tuple(refs)
            scene.step_proposals.append(proposal)
        elif kind == "INTENT":
            require(len(columns) == 25, "INTENT shape")
            (_, _, at, vessel, equipment, radio, generation, decision, proposal, channel,
             slot, start, nss, first, ordinal, successor, raw, keep, length, status,
             message, revision, fingerprint, ledgers, reason) = columns
            (at, vessel, equipment, radio, generation, decision, proposal, slot, start,
             nss, first, ordinal, raw, keep, length, revision, fingerprint) = map(int,
                (at, vessel, equipment, radio, generation, decision, proposal, slot, start,
                 nss, first, ordinal, raw, keep, length, revision, fingerprint))
            successor = nullable(successor)
            scene.at(at)
            require(scene.on and generation == scene.generation and (vessel, equipment, radio) == scene.owner,
                    "intent foreign owner/epoch")
            require(channel in scene.channels and scene.rate is not None, "intent channel/rate")
            model = scene.channels[channel]
            require(model.next_due == slot and model.first == first and model.nss == nss and model.done == ordinal,
                    "intent is not the due channel chain slot")
            require(at == start == scene.start(slot) and at >= scene.on_at + FRAME, "intent exact own-anchor slot start")
            terminal = ordinal == scene.rate // 2
            if terminal:
                expected_next = None
                require(slot == first + FRAME_SLOTS, "terminal zero is not one frame after first NTS")
                counts["zeros"] += 1
            elif ordinal == scene.rate // 2 - 1:
                expected_next = first + FRAME_SLOTS
            else:
                expected_next = model.prepared
                require(expected_next is not None, "nonterminal intent lacks selected successor")
            require(successor == expected_next, "announced successor differs from actual selected/kept slot")
            increment = 0 if terminal else expected_next - slot
            require((raw, keep, length) == (raw_itdma(increment), 1, 1), "independent ITDMA word/keep/one-slot count")
            expected_message = "type3terminal" if terminal else "type3entry" if ordinal == 0 else "type3firstframe"
            require(normalized(message) == expected_message and normalized(status) == "due", "Message3 flavor or due status")
            require(revision > 0 and fingerprint == scene.fingerprint(at) and reason, "intent own-input provenance")
            require((generation, decision) not in scene.decision_ids and decision > 0, "duplicate due decision ID")
            require(proposal in scene.proposals and proposal not in scene.consumed_proposals and
                    scene.proposals[proposal][:4] == (generation, channel, slot, ordinal), "due intent proposal identity")
            refs = [] if ledgers == "-" else list(map(int, ledgers.split(",")))
            require(all(ref in scene.draws and scene.draws[ref][0] == generation for ref in refs), "intent draw ledger lineage")
            successor_refs = () if terminal else (model.first_refs if ordinal == scene.rate // 2 - 1
                                                  else model.prepared_refs)
            expected_refs = sorted(set(scene.proposals[proposal][4] + successor_refs))
            require(refs == expected_refs and refs, "intent exact current/successor draw lineage")
            for past_generation, past_channel, past_slot, *_ in scene.due:
                if past_generation == generation and past_channel != channel:
                    require(abs(slot - past_slot) > 1, "one-transmitter opposite-channel adjacency")
            scene.decision_ids.add((generation, decision))
            scene.consumed_proposals.add(proposal)
            scene.due.append((generation, channel, slot, ordinal, raw))
            scene.step_due.append(decision)
            model.done += 1
            model.prepared = None
            model.prepared_refs = ()
            model.next_due = expected_next
            model.phase = "continuoussotdmamarker" if terminal else "firstframechain"
            counts["intents"] += 1
        elif kind == "STEP":
            require(len(columns) == 8, "STEP shape")
            _, _, at, generation, state, reason, due, proposed = columns
            at, generation, due, proposed = map(int, (at, generation, due, proposed))
            scene.at(at)
            require(generation == scene.generation, "step epoch")
            require((due, proposed) == (len(scene.step_due), len(scene.step_proposals)), "step output counts")
            scene.step_due.clear()
            scene.step_proposals.clear()
            state = normalized(state)
            require(state in ("off", "observe60seconds", "entryselect", "entrywait", "firstframe", "continuousmarker", "blocked"), "state domain")
            if state == "continuousmarker":
                require(all(item.phase == "continuoussotdmamarker" for item in scene.channels.values()), "aggregate continuous before two zeros")
            if state == "off":
                require(not scene.on and due == 0, "OFF step emitted an intent")
            if state == "blocked":
                require(due == 0 and reason not in ("", "none"), "blocked step needs typed reason and zero intents")
                if scene.rate_numerator < 2 * scene.rate_denominator:
                    require(reason == "blocked_low_rate_policy_absent", "low-rate policy silently replaced")
            if scene.on and at < scene.on_at + FRAME:
                require(state == "observe60seconds" and due == proposed == 0, "pre-60-second protocol work")
            scene.state = state
        elif kind == "PLAN":
            require(len(columns) == 10, "PLAN shape")
            _, _, at, generation, channel, nss, first, next_due, done, phase = columns
            at, generation, done = map(int, (at, generation, done))
            scene.at(at)
            require(generation == scene.generation and channel in scene.channels, "plan owner epoch/channel")
            expected = scene.channels[channel]
            require((nullable(nss), nullable(first), nullable(next_due), done, normalized(phase)) ==
                    (expected.nss, expected.first, expected.next_due, expected.done, expected.phase), "channel plan differs from independent chain")
            counts["plans"] += 1
        elif kind == "WITHDRAW":
            require(len(columns) == 7, "WITHDRAW shape")
            _, _, at, generation, channel, slot, reason = columns
            scene.at(int(at))
            key = int(generation), channel, int(slot)
            require(key in scene.withdrawal_expected and reason == "cancelled_by_off", "withdrawal without OFF/pending proposal")
            scene.withdrawal_expected.remove(key)
        elif kind == "END":
            require(len(columns) == 7, "END shape")
            _, _, state, due, zero_a, zero_b, ledger_count = columns
            require(normalized(state) == scene.state and int(due) == len(scene.due), "END state/intent count")
            zeros = {channel: sum(row[1] == channel and row[4] == 1 for row in scene.due)
                     for channel in ("AIS1", "AIS2")}
            require((int(zero_a), int(zero_b)) == (zeros["AIS1"], zeros["AIS2"]), "END separate channel zeros")
            require(int(ledger_count) == len(scene.draws), "END exported draw ledger count")
            require(not scene.step_due and not scene.step_proposals and not scene.withdrawal_expected,
                    "incomplete step/withdrawal trace")
            if scene.state == "continuousmarker":
                require(zeros["AIS1"] == zeros["AIS2"] == 1, "completed scene needs exactly two independent terminal zeros")
            scene.ended = True
        else:
            raise AssertionError(f"unknown trace row {kind}")
    require(scenes and all(scene.ended for scene in scenes.values()), "missing complete scene END")
    require({scene.rate for scene in scenes.values() if scene.rate is not None} >= {6, 10, 18, 30}, "missing supported-rate trace")
    verify_required_fixtures(scenes)
    return counts, scenes


def replace_trace_field(data, kind, scene, column, transform, predicate=lambda _: True):
    """Alter one scalar while retaining every row and its native line ending."""
    lines = data.decode("ascii").splitlines(keepends=True)
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        columns = body.split("|")
        if columns[0] == kind and columns[1] == scene and predicate(columns):
            columns[column] = transform(columns[column])
            lines[index] = "|".join(columns) + line[len(body):]
            return "".join(lines).encode("ascii")
    raise AssertionError(f"missing {kind}/{scene} corruption witness target")


def rejection_witnesses(native):
    mutations = {
        "terminal_zero": replace_trace_field(native, "INTENT", "F1", 16, lambda _: "17",
                                              lambda row: normalized(row[20]) == "type3terminal"),
        "own_anchor": replace_trace_field(native, "SCENE", "F1", 7, lambda value: str(int(value) + 1)),
        "due_instant": replace_trace_field(native, "INTENT", "F1", 11, lambda value: str(int(value) + 1)),
        "philox_word": replace_trace_field(native, "DRAW", "F1", 11, lambda value: str(int(value) ^ 1)),
        "draw_lineage": replace_trace_field(native, "INTENT", "F1", 23, lambda _: "-"),
    }
    rejected = []
    for name, corrupted in mutations.items():
        require(corrupted != native, f"{name} witness did not change trace")
        try:
            verify(corrupted)
        except AssertionError:
            rejected.append(name)
        else:
            raise AssertionError(f"oracle accepted corrupted {name}")
    return rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    args = parser.parse_args()
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES="-1")
    outputs = [subprocess.run([args.exe, "--vectors"], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environment).stdout for _ in range(2)]
    require(outputs[0] == outputs[1], "fresh-process trace changed")
    counts, scenes = verify(outputs[0])
    native = outputs[0]
    rejected = rejection_witnesses(native)
    canonical = native.replace(b"\r\n", b"\n")
    print(f"PASS independent access oracle: scenes={len(scenes)} rows={counts['rows']} "
          f"draws={counts['draws']} due_intents={counts['intents']} terminal_zeros={counts['zeros']} "
          f"channel_plans={counts['plans']} fresh_processes=2")
    print(f"REJECT corruption_witnesses={len(rejected)} names={','.join(rejected)}")
    print(f"TRACE native_bytes={len(native)} native_sha256={hashlib.sha256(native).hexdigest()} "
          f"lf_bytes={len(canonical)} lf_sha256={hashlib.sha256(canonical).hexdigest()}")


if __name__ == "__main__":
    main()
