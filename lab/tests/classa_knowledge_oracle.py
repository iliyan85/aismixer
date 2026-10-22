"""Independent, standard-library A1-S3 knowledge/candidate oracle.

The A1-S3 vectors are synthetic Class A knowledge fixtures (SIMULATION_POLICY
CLASSA-PHILOX-v1 and friends), not RF observations. This checker runs the test
executable with ``--vectors`` and recomputes every row from the written
definitions alone: its own Philox4x32-10, draw packing, bounded rejection draw,
LCP-P01/P02/P04/P05 arithmetic (RATDMA terms are arbitrary-precision Fractions),
expiry/range/position conversions, absolute slot arithmetic and the candidate hierarchy
(future-only: a block containing the decision slot is never a candidate). No production source under lab/classa,
lab/ais, lab/runtime or the C++ test file is imported, read or inspected; exact
rationals use Fraction, never floats.
"""

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
import hashlib
import os
import re
import struct
import subprocess


HEADER = "A1-S3|SYNTHETIC_KNOWLEDGE_FIXTURE|1"
MASK32 = 0xFFFFFFFF
FRAME_SLOTS = 2250
MAX_REJECTIONS = 64
INTERVALS = (75, 125, 225, 375)
RANGE_LIMIT_UM = 222_240_000_000
SEVERITY = {"F1": 1, "F2": 2, "A1": 3, "A2": 4, "U": 5, "O": 6, "X": 7}
CLASS_OF = {severity: name for name, severity in SEVERITY.items()}
DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)\Z")
HEXWORD = re.compile(r"[0-9a-f]{8}\Z")
MIN_ROWS = {
    "PHILOX": 3, "PACK": 10, "DRAW": 40, "SI": 8, "SIREJECT": 4, "ADJ": 6, "RATDMA": 101,
    "TIMEOUT": 5, "EXPIRY": 3, "RANGE": 3, "POS": 6, "ABS": 10, "ABSOFF": 6, "CAND": 12,
    "CHOICE": 4,
}
PHILOX_KATS = (
    ((0, 0, 0, 0), (0, 0), (0x6627E8D5, 0xE169C58D, 0xBC57AC4C, 0x9B00DBD8)),
    ((MASK32,) * 4, (MASK32,) * 2, (0x408F276D, 0x41C83B0E, 0xA20BC7C6, 0x6D5451FD)),
    ((0x243F6A88, 0x85A308D3, 0x13198A2E, 0x03707344), (0xA4093822, 0x299F31D0),
     (0xD16CFE09, 0x94FDCCEB, 0x5001E420, 0x24126EA1)),
)
RATDMA_LITERALS = (
    ((4, 0, Fraction(25), 25), ("would_request", Fraction(25), 3, 0)),
    ((4, 0, Fraction(25), 26), ("inhibited", Fraction(175, 4), 3, 1)),
    ((3, 1, Fraction(175, 4), 43), ("would_request", Fraction(175, 4), 2, 1)),
    ((3, 1, Fraction(175, 4), 44), ("inhibited", Fraction(125, 2), 2, 2)),
    ((150, 0, Fraction(2, 3), 1), ("inhibited", Fraction(299, 225), 149, 1)),
)


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def decimal(text):
    require(DECIMAL.match(text) is not None, f"not a canonical decimal: {text!r}")
    return int(text)


def hexword(text):
    require(HEXWORD.match(text) is not None, f"not an 8-digit lowercase hex word: {text!r}")
    return int(text, 16)


def philox(counter, key):
    """Philox4x32-10 exactly as written in the A1-S3 definitions (no library RNG)."""
    c, k = list(counter), list(key)
    require(len(c) == 4 and len(k) == 2 and all(0 <= w <= MASK32 for w in c + k), "Philox words")
    for _ in range(10):
        product0, product2 = 0xD2511F53 * c[0], 0xCD9E8D57 * c[2]
        c = [(product2 >> 32) ^ c[1] ^ k[0], product2 & MASK32,
             (product0 >> 32) ^ c[3] ^ k[1], product0 & MASK32]
        k = [(k[0] + 0x9E3779B9) & MASK32, (k[1] + 0xBB67AE85) & MASK32]
    return c


def pack(seed, equipment, generation, purpose, episode, draw, rejection):
    """Key and counter words of a version-1 draw position."""
    domain = ((seed, 0, (1 << 64) - 1), (equipment, 1, MASK32), (generation, 1, 0xFFFFFF),
              (purpose, 101, 105), (episode, 0, MASK32), (draw, 0, 0xFFFF), (rejection, 0, 0xFF))
    for value, low, high in domain:
        require(low <= value <= high, f"draw position input {value} outside {low}..{high}")
    key = (seed & MASK32, seed >> 32)
    counter = (equipment, episode, generation | (purpose << 24),
               draw | (rejection << 16) | (1 << 24))
    return key, counter


def bounded_draw(seed, equipment, generation, purpose, episode, draw, low, high):
    """Return (span, limit, proposal blocks in order, value); the last block is accepted."""
    require(low <= high, "empty draw range")
    span = high - low + 1
    require(span <= 1 << 32, "draw range wider than 2^32")
    limit = ((1 << 32) // span) * span
    proposals = []
    for rejection in range(MAX_REJECTIONS + 1):
        key, counter = pack(seed, equipment, generation, purpose, episode, draw, rejection)
        proposals.append(philox(counter, key))
        if proposals[-1][0] < limit:
            return span, limit, proposals, low + proposals[-1][0] % span
    raise AssertionError("bounded draw exhausted its rejection budget")


def selection_interval(numerator, denominator):
    """NI as an integer when numerator/denominator reduces to 75/125/225/375, else None."""
    ni = Fraction(numerator, denominator) if denominator else None
    accepted = ni is not None and ni.denominator == 1 and ni.numerator in INTERVALS
    return ni.numerator if accepted else None


def interval_bounds(ns, ni):
    half = ni // 10
    return half, ns - half, ns + half, 2 * half + 1


def own_adjacency(first, length, frames):
    require(1 <= length <= 5 and 0 <= frames <= 7, "own block length 1..5, retained frames 0..7")
    occupied = {first + frame * FRAME_SLOTS + offset
                for frame in range(frames + 1) for offset in range(length)}
    return sorted({j + d for j in occupied for d in (-1, 0, 1)})


def ratdma_attempt(candidates, attempts, p, u):
    """LCP-P04 generic RATDMA attempt on exact rationals, never clamped."""
    require(candidates >= 1 and attempts >= 0 and 0 <= u <= 100, "RATDMA attempt domain")
    if u <= p:
        return "would_request", p, candidates - 1, attempts
    return "inhibited", p + (100 - p) / candidates, candidates - 1, attempts + 1


def nearest(value):
    """Nearest integer of an exact rational whose remainder is never one half."""
    whole, rest = divmod(value.numerator, value.denominator)
    require(2 * rest != value.denominator, "a half remainder cannot occur here")
    return whole + (2 * rest > value.denominator)


def parse_token(text):
    """(class name, dist) of a per-slot token; dist is 0 unless the token carries @dist."""
    name, marker, dist = text.partition("@")
    require(name in SEVERITY, f"unknown class token {text!r}")
    if name not in ("A1", "A2"):
        require(marker == "", f"{name} tokens carry no @dist")
        return name, 0
    require(marker == "@" and decimal(dist) >= 0, "A1/A2 tokens carry a non-negative @dist")
    return name, int(dist)


def candidate_hierarchy(first, length, now, xtokens, ytokens):
    """Admitted [(index, tier, xclass, yclass)] by index, free_both and reached."""
    width = len(xtokens)
    # Most-distant pool: reusable holders at slots that could still be chosen (after the decision slot,
    # not blocked on the selection channel). A1 marks exactly the pool maximum, on either channel.
    pool = [dist for i in range(width) if first + i > now and SEVERITY[xtokens[i][0]] <= 4
            for name, dist in (xtokens[i], ytokens[i]) if name in ("A1", "A2")]
    top = max(pool) if pool else None
    for name, dist in xtokens + ytokens:  # A1 marks exactly the pool maximum, wherever that distance occurs.
        if name in ("A1", "A2"):
            require((name == "A1") == (top is not None and dist == top), "A1 must mark exactly the pool-maximum reuse distance")
    # LCP-P05: every own slot on the other channel excludes j-1, j, j+1 on the selection channel, and an X
    # needs an own slot within one slot (or sits at an interval edge where the own slot may lie outside).
    for i, (name, _) in enumerate(ytokens):
        if name == "O":
            for j in (i - 1, i, i + 1):
                if 0 <= j < width:
                    require(xtokens[j][0] == "X", "own slot on the other channel must exclude its neighbours")
    for i, (name, _) in enumerate(xtokens):
        if name == "X":
            near_own = any(0 <= j < width and ytokens[j][0] == "O" for j in (i - 1, i, i + 1))
            require(near_own or i in (0, width - 1), "X requires an own slot within one slot")
    entries = []
    for start in range(width - length + 1):
        index = first + start  # the first slot of a block is its smallest absolute index
        xblock, yblock = xtokens[start:start + length], ytokens[start:start + length]
        if index <= now or any(SEVERITY[name] > 4 for name, _ in xblock):
            continue  # future-only: the decision slot itself is never a candidate
        if index // FRAME_SLOTS != (index + length - 1) // FRAME_SLOTS:
            continue  # frame-contained blocks only (A0-MAC-013)
        xclass, xdist = max(xblock, key=lambda token: SEVERITY[token[0]])  # first of equal worst
        yworst = max(yblock, key=lambda token: min(SEVERITY[token[0]], 5))  # first of equal worst, U-capped
        ysev = min(SEVERITY[yworst[0]], 5)
        yclass = CLASS_OF[ysev]
        ydist = yworst[1] if ysev in (3, 4) else 0
        xfree, yfree, yavail = SEVERITY[xclass] <= 2, ysev <= 2, ysev in (3, 4)
        if yfree or yavail:  # tiers 0/1 when X is free, 2/3 otherwise; +1 when Y is available
            tier = (0 if xfree else 2) + (0 if yfree else 1)
        else:
            tier = 4 if xfree else 5  # yworst is U
        subrank = (2 if xclass in ("F2", "A2") else 0) + (1 if yclass in ("F2", "A2") else 0)
        entries.append((tier, subrank, -xdist, -ydist, index, xclass, yclass))
    entries.sort()
    free_both = sum(entry[0] == 0 for entry in entries)
    # Every tier-0 block, then tiers 1..5 in order until four blocks are admitted.
    admitted = sorted(entries[:max(free_both, 4)], key=lambda entry: entry[4])
    result = [(index, tier, xclass, yclass) for tier, _, _, _, index, xclass, yclass in admitted]
    return result, free_both, len(admitted) >= 4


def check_philox(fields):
    require(len(fields) == 11, "PHILOX row shape")
    words = [hexword(word) for word in fields[1:]]
    require(philox(words[:4], words[4:6]) == words[6:], "Philox4x32-10 output words")
    return tuple(words[:6])


def check_pack(fields):
    require(len(fields) == 15, "PACK row shape")
    key, counter = pack(*[decimal(field) for field in fields[1:8]])
    require([hexword(word) for word in fields[8:14]] == list(key + counter), "PACK words")
    require(fields[14] == struct.pack("<6I", *(key + counter)).hex(), "PACK 24-byte serialization")


def check_draw(fields):
    require(len(fields) == 15, "DRAW row shape")
    numbers = [decimal(field) for field in fields[1:14]]
    span, limit, proposals, value = bounded_draw(*numbers[:8])
    listed = []
    for entry in fields[14].split(";"):
        rejection, words = entry.split(":")
        listed.append((decimal(rejection), [decimal(word) for word in words.split(",")]))
    require(listed == list(enumerate(proposals)), "DRAW proposal blocks")
    require(numbers[8:] == [span, limit, len(proposals) - 1, proposals[-1][0], value],
            "DRAW span/limit/rejections/accepted_word/value")


def check_si(fields):
    require(len(fields) == 8, "SI row shape")
    ns, numerator, denominator, half, first, last, width = [decimal(f) for f in fields[1:]]
    ni = selection_interval(numerator, denominator)
    require(ni is not None, "SI row carries an NI the LCP-P01 rule rejects")
    require((half, first, last, width) == interval_bounds(ns, ni), "SI bounds")
    return ns, ni


def check_sireject(fields):
    require(len(fields) == 3, "SIREJECT row shape")
    require(selection_interval(*map(decimal, fields[1:])) is None, "SIREJECT names an acceptable NI")


def check_adj(fields):
    require(len(fields) == 5, "ADJ row shape")
    excluded = [decimal(index) for index in fields[4].split(",")]
    require(excluded == own_adjacency(*[decimal(f) for f in fields[1:4]]), "ADJ excluded set")


def check_ratdma(fields):
    require(len(fields) == 11, "RATDMA row shape")
    candidates, attempts, p_num, p_den, u = [decimal(f) for f in fields[1:6]]
    p2_num, p2_den, candidates2, attempts2 = [decimal(f) for f in fields[7:]]
    require(p_den > 0 and p2_den > 0, "RATDMA probability denominators")
    require(fields[6] in ("would_request", "inhibited"), "RATDMA outcome token")
    key = (candidates, attempts, Fraction(p_num, p_den), u)
    result = ratdma_attempt(*key)
    require((fields[6], Fraction(p2_num, p2_den), candidates2, attempts2) == result,
            "RATDMA transition")
    return key, result


def check_timeout(fields):
    require(len(fields) == 3, "TIMEOUT row shape")
    initial = decimal(fields[1])
    require(3 <= initial <= 7, "initial timeout in 3..7")
    sequence = list(range(initial - 1, -1, -1))  # one decrement per visit; T values ending in 0
    require([decimal(v) for v in fields[2].split(",")] == sequence, "TIMEOUT visit sequence")
    return initial


def check_expiry(fields):
    require(len(fields) == 6, "EXPIRY row shape")
    old, new, offset, sync, raw = [decimal(f) for f in fields[1:]]
    require(new // FRAME_SLOTS == old // FRAME_SLOTS + 1, "EXPIRY announces the next frame")
    require(0 <= sync <= 3, "EXPIRY sync state is two bits")
    expected = new - old
    require(expected == FRAME_SLOTS + new % FRAME_SLOTS - old % FRAME_SLOTS
            and 1 <= expected <= 4499, "EXPIRY offset identity")
    require(offset == expected and raw == (sync << 17) | (0 << 14) | offset, "EXPIRY offset/raw")


def check_range(fields):
    require(len(fields) == 3, "RANGE row shape")
    distance = decimal(fields[1])
    require(fields[2] == ("beyond" if distance > RANGE_LIMIT_UM else "within"), "RANGE class")
    return distance


def check_pos(fields):
    require(len(fields) == 5, "POS row shape")
    lon, lat, lon_ndeg, lat_ndeg = [decimal(f) for f in fields[1:]]
    expected = (nearest(Fraction(lon * 5000, 3)), nearest(Fraction(lat * 5000, 3)))
    require((lon_ndeg, lat_ndeg) == expected, "POS nanodegrees")


def check_abs(fields):
    require(len(fields) == 4, "ABS row shape")
    minute, slot, index = [decimal(f) for f in fields[1:]]
    require(0 <= slot < FRAME_SLOTS and index == minute * FRAME_SLOTS + slot
            and divmod(index, FRAME_SLOTS) == (minute, slot), "ABS index and inverse")


def check_absoff(fields):
    require(len(fields) == 6, "ABSOFF row shape")
    index, delta, result, minute, slot = [decimal(f) for f in fields[1:]]
    require(result == index + delta and divmod(result, FRAME_SLOTS) == (minute, slot),
            "ABSOFF result and split")


def parse_candidates(text):
    listed = []
    for entry in text.split(";") if text else ():
        index, tier, xclass, yclass, peer = entry.split(":")
        expected_peer = peer == "-" if xclass in ("F1", "F2") else re.fullmatch(r"[0-9]+(\+[0-9]+)*", peer) is not None
        require(expected_peer, "candidate peer is - for F1/F2 and +-joined decimal MMSIs for A1/A2")
        listed.append((decimal(index), decimal(tier), xclass, yclass))
    return listed


def check_cand(fields):
    require(len(fields) == 14, "CAND row shape")
    length, ns, ni, now, own_usable = [decimal(f) for f in fields[3:8]]
    require(1 <= length <= 5 and ni in INTERVALS and own_usable in (0, 1), "CAND parameters")
    _, first, _, width = interval_bounds(ns, ni)
    xtokens = [parse_token(token) for token in fields[8].split(",")]
    ytokens = [parse_token(token) for token in fields[9].split(",")]
    require(len(xtokens) == width and len(ytokens) == width, "CAND token count = interval width")
    require(all(token[0] != "X" for token in ytokens), "X tokens belong to the selection channel")
    admitted, free_both, reached = candidate_hierarchy(first, length, now, xtokens, ytokens)
    require(parse_candidates(fields[10]) == admitted, "CAND admitted candidate list")
    fallback = "none" if admitted else ("current_slot_usable" if own_usable else "no_reservation")
    expected = (free_both, str(int(reached)), fallback)
    require((decimal(fields[11]), fields[12], fields[13]) == expected, "CAND free_both/reached/fallback")
    return fields[1], [index for index, _, _, _ in admitted]


def check_choice(fields, candidates):
    require(len(fields) == 10, "CHOICE row shape")
    seed, equipment, generation, episode, draw, n, value, chosen = [decimal(f) for f in fields[2:]]
    require(fields[1] in candidates, f"CHOICE {fields[1]!r} has no CAND row")
    indices = candidates[fields[1]]
    require(n == len(indices), "CHOICE n equals the admitted candidate count")
    require(value == bounded_draw(seed, equipment, generation, 102, episode, draw, 0, n - 1)[3],
            "CHOICE draw value")
    require(chosen == indices[value], "CHOICE chosen_index is the value-th admitted candidate")


def check_ratdma_table(table):
    """Cross-row RATDMA requirements: the C=4/A=0/p=25 sweep and the literal rows."""
    sweep = {u for (candidates, attempts, p, u) in table if (candidates, attempts, p) == (4, 0, 25)}
    require(sweep == set(range(101)), "RATDMA sweep must cover u = 0..100")
    requests = sum(table[(4, 0, Fraction(25), u)][0] == "would_request" for u in sweep)
    require(requests == 26, f"RATDMA sweep would_request count {requests} != 26")
    for key, result in RATDMA_LITERALS:
        require(table.get(key) == result, f"RATDMA literal row {key} absent or wrong")
    # Exact terms wider than 64 bits must be present (interleaved removal/inhibition sequences).
    widest = max(result[1].denominator for result in table.values())
    require(widest > 2 ** 64, f"RATDMA rows never exceed 64-bit terms (widest denominator {widest})")


CHECKERS = {
    "PHILOX": check_philox, "PACK": check_pack, "DRAW": check_draw, "SI": check_si,
    "SIREJECT": check_sireject, "ADJ": check_adj, "RATDMA": check_ratdma,
    "TIMEOUT": check_timeout, "EXPIRY": check_expiry, "RANGE": check_range, "POS": check_pos,
    "ABS": check_abs, "ABSOFF": check_absoff, "CAND": check_cand, "CHOICE": lambda fields: fields,
}


def verify(data):
    """Verify a whole --vectors output (bytes or text) in any row order; raise on inconsistency."""
    text = data.decode("ascii") if isinstance(data, bytes) else data
    lines = text.splitlines()
    require(lines and lines[0] == HEADER, "fixture header/version")
    families, facts = Counter(), defaultdict(list)
    for line in lines[1:]:
        fields = line.split("|")
        require(fields[0] in CHECKERS, f"unknown fixture family {fields[0]!r}")
        facts[fields[0]].append(CHECKERS[fields[0]](fields))
        families[fields[0]] += 1
    candidates = {}
    for name, indices in facts["CAND"]:
        require(name not in candidates, f"duplicate CAND name {name!r}")
        candidates[name] = indices
    require("now_inside_si_most_distant" in candidates and "two_slot_two_peers" in candidates,
            "future-only and multi-peer candidate scenes absent")
    for fields in facts["CHOICE"]:  # resolved by name once every CAND row has been read
        check_choice(fields, candidates)
    check_ratdma_table(dict(facts["RATDMA"]))
    require((1000, 375) in facts["SI"], "SI literal NS 1000 / NI 375 (last 1037) absent")
    require({3, 7} <= set(facts["TIMEOUT"]), "TIMEOUT rows for initial 3 and 7 absent")
    boundary = {RANGE_LIMIT_UM - 1, RANGE_LIMIT_UM, RANGE_LIMIT_UM + 1}
    require(boundary <= set(facts["RANGE"]), "RANGE boundary rows absent")
    for family, minimum in MIN_ROWS.items():
        require(families[family] >= minimum, f"insufficient {family} coverage: < {minimum}")
    return families


def mutate(text, family, transform, match=None, duplicate=False):
    """Rewrite (or, with duplicate, keep and append a rewritten copy of) the first matching row."""
    lines = text.splitlines()
    for position, line in enumerate(lines):
        fields = line.split("|")
        if fields[0] == family and (match is None or match(fields)):
            mutated = "|".join(transform(fields))
            require(mutated != line, "corruption left the row unchanged")
            lines[position:position + 1] = [line, mutated] if duplicate else [mutated]
            return "\n".join(lines) + "\n"
    raise AssertionError(f"no {family} row available for corruption")


def edit(fields, position, value):
    fields[position] = value
    return fields


def bump(fields, position):
    return edit(fields, position, str(decimal(fields[position]) + 1))


def flipped(hexdigits):
    """The hex string with the lowest bit of its last digit (hence one byte) flipped."""
    return hexdigits[:-1] + format(int(hexdigits[-1], 16) ^ 1, "x")


SWAPPED = {"would_request": "inhibited", "inhibited": "would_request",
           "within": "beyond", "beyond": "within"}
KAT_INPUTS = {counter + key for counter, key, _ in PHILOX_KATS}
CORRUPTIONS = (  # family, row filter, mutation, append the mutated copy instead of replacing
    ("PHILOX", None, lambda f: edit(f, 7, flipped(f[7])), False),
    ("PACK", None, lambda f: edit(f, 14, flipped(f[14])), False),
    ("DRAW", None, lambda f: bump(f, 13), False),
    ("DRAW", None, lambda f: bump(f, 11), False),
    ("SI", lambda f: f[1] == "1000" and f[6] == "1037", lambda f: bump(f, 6), False),  # NI 375
    ("ADJ", None, lambda f: edit(f, 4, f[4].partition(",")[2]), False),
    ("RATDMA", None, lambda f: edit(f, 6, SWAPPED[f[6]]), False),
    ("RATDMA", None, lambda f: bump(f, 7), False),
    ("TIMEOUT", None, lambda f: edit(f, 2, f[2] + ",0"), False),
    ("RANGE", None, lambda f: edit(f, 2, SWAPPED[f[2]]), False),
    ("CAND", lambda f: f[10] != "", lambda f: edit(f, 10, f[10].partition(";")[2]), False),
    ("CHOICE", None, lambda f: bump(f, 9), False),
    ("A1-S3", None, lambda f: edit(f, 2, "2"), False),
    ("PHILOX", lambda f: tuple(map(hexword, f[1:7])) in KAT_INPUTS,
     lambda f: edit(f, 7, flipped(f[7])), True),
)


def corruption_checks(text):
    """Exercise the verifier's failure paths on real rows; return the rejection count."""
    rejected = 0
    for family, match, transform, duplicate in CORRUPTIONS:
        corrupted = mutate(text, family, transform, match, duplicate)
        try:
            verify(corrupted)
        except (AssertionError, ValueError):
            rejected += 1
        else:
            raise AssertionError(f"independent oracle accepted a corrupted {family} row")
    return rejected


def self_test():
    """Assert this oracle's own Philox and RATDMA arithmetic on the written known answers."""
    for counter, key, expected in PHILOX_KATS:
        require(tuple(philox(counter, key)) == expected, "Philox4x32-10 known answer")
    for (candidates, attempts, p, u), result in RATDMA_LITERALS:
        require(ratdma_attempt(candidates, attempts, p, u) == result, "RATDMA literal transition")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exe", required=True, help="path to aismixer_lab_classa_knowledge_tests")
    args = parser.parse_args()
    self_test()
    runs = [subprocess.run([args.exe, "--vectors"], check=True, capture_output=True,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, CUDA_VISIBLE_DEVICES="-1"))
            .stdout for _ in range(2)]
    require(runs[0] == runs[1], "fresh-process fixture output differs")
    families = verify(runs[0])
    rejected = corruption_checks(runs[0].decode("ascii"))
    print(f"A1-S3 independent oracle PASS: {sum(families.values())} rows, "
          f"{len(families)} families, {rejected} corrupted rows rejected, 2 fresh processes; "
          f"{len(runs[0])} bytes SHA256={hashlib.sha256(runs[0]).hexdigest()}")


if __name__ == "__main__":
    main()
