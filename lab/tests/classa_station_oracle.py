"""Independent, standard-library A1-S2 arithmetic oracle.

These are synthetic station-input fixtures, not RF observations. Reference
arithmetic uses Fraction and divmod; no production conversion helper, encoder,
clock implementation, or third-party AIS decoder is imported.
"""

import argparse
from fractions import Fraction
import hashlib
import subprocess


SECOND = 9_830_400
FRAME = 589_824_000
SLOT = 262_144


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def nearest(value):
    """Integer nearest; exact half ties away from zero, using exact rationals."""
    sign = -1 if value < 0 else 1
    magnitude = abs(value)
    whole, rest = divmod(magnitude.numerator, magnitude.denominator)
    return sign * (whole + (rest * 2 >= magnitude.denominator))


def reference_quantization(kind, source):
    value = None if source == "UNKNOWN" else int(source)
    if kind in ("longitude", "latitude"):
        require(value is not None, "coordinate cannot be implicitly unknown")
        bound = (180 if kind == "longitude" else 80) * 1_000_000_000
        require(-bound <= value <= bound, "unqualified W1 coordinate")
        return nearest(Fraction(value * 3, 5000))
    if kind == "speed":
        if value is None:
            return 1023
        require(value >= 0, "negative speed")
        return min(1022, nearest(Fraction(value, 100)))
    if kind == "course":
        if value is None:
            return 3600
        require(0 <= value < 360_000_000_000, "invalid ground course")
        return nearest(Fraction(value, 100_000_000)) % 3600
    raise AssertionError("unknown quantization kind")


def reference_clock(instant, anchor_seconds, anchor_phase):
    require(0 <= anchor_phase < SECOND, "unnormalized UTC anchor")
    # Python arbitrary-width arithmetic independently checks the runtime's
    # checked int64 implementation without reproducing its overflow strategy.
    absolute = anchor_seconds * SECOND + anchor_phase + instant
    minute, in_minute = divmod(absolute, FRAME)
    slot, phase = divmod(in_minute, SLOT)
    second = in_minute // SECOND
    return minute, slot, phase, second


def verify(data):
    rows = data.decode("ascii").splitlines()
    require(rows and rows[0] == "A1-S2|SYNTHETIC_INPUT_FIXTURE|1", "fixture version/provenance")
    quantization = {}
    clocks = {}
    for line in rows[1:]:
        columns = line.split("|")
        if columns[0] == "QUANT":
            require(len(columns) == 4, "quantization row shape")
            _, kind, source, actual = columns
            key = kind, source
            require(key not in quantization, "duplicate quantization fixture")
            expected = reference_quantization(kind, source)
            require(int(actual) == expected, f"{key}: {actual} != {expected}")
            quantization[key] = int(actual)
        elif columns[0] == "UTC":
            require(len(columns) == 8, "UTC row shape")
            numbers = tuple(map(int, columns[1:]))
            key = numbers[:3]
            require(key not in clocks, "duplicate UTC fixture")
            require(numbers[3:] == reference_clock(*key), f"UTC {key}: wrong minute/slot/phase/second")
            clocks[key] = numbers[3:]
        else:
            raise AssertionError("unexpected fixture record")

    # Literal hand-worked examples make accidental shared arithmetic errors
    # visible: 3*2500/5000=1.5, 359.95deg rounds to 360.0 then wraps, and
    # one unit before UTC zero is minute -1, final slot, final phase.
    literal_quant = {
        ("longitude", "2500"): 2,
        ("longitude", "-2500"): -2,
        ("latitude", "80000000000"): 48000000,
        ("longitude", "-180000000000"): -108000000,
        ("speed", "50"): 1,
        ("speed", "102250"): 1022,
        ("speed", "UNKNOWN"): 1023,
        ("course", "359950000000"): 0,
        ("course", "UNKNOWN"): 3600,
    }
    for key, expected in literal_quant.items():
        require(quantization.get(key) == expected, f"missing/wrong literal {key}")
    literal_clock = {
        (0, 0, 0): (0, 0, 0, 0),
        (-1, 0, 0): (-1, 2249, 262143, 59),
        (589561856, 0, 0): (0, 2249, 0, 59),
        (589824000, 0, 0): (1, 0, 0, 0),
        (0, -1, 0): (-1, 2212, 131072, 59),
        (0, 59, 1): (0, 2212, 131073, 59),
    }
    for key, expected in literal_clock.items():
        require(clocks.get(key) == expected, f"missing/wrong literal UTC {key}")
    require(len(quantization) >= 90 and len(clocks) >= 20, "insufficient arithmetic boundary coverage")
    return len(quantization), len(clocks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    args = parser.parse_args()
    first = subprocess.run([args.exe, "--vectors"], check=True, capture_output=True).stdout
    second = subprocess.run([args.exe, "--vectors"], check=True, capture_output=True).stdout
    require(first == second, "fresh-process fixture output differs")
    quant, clocks = verify(first)
    # Exercise this independent verifier's actual failure paths on real rows.
    rejected = 0
    for before, after in (
        (b"QUANT|longitude|2500|2", b"QUANT|longitude|2500|1"),
        (b"QUANT|speed|UNKNOWN|1023", b"QUANT|speed|UNKNOWN|1022"),
        (b"QUANT|course|359950000000|0", b"QUANT|course|359950000000|3600"),
        (b"UTC|-1|0|0|-1|2249|262143|59", b"UTC|-1|0|0|0|2249|262143|59"),
        (b"UTC|589824000|0|0|1|0|0|0", b"UTC|589824000|0|0|1|0|1|0"),
    ):
        require(before in first, "corruption fixture absent")
        try:
            verify(first.replace(before, after, 1))
        except (AssertionError, ValueError):
            rejected += 1
        else:
            raise AssertionError("independent oracle accepted corrupted arithmetic fixture")
    print(f"A1-S2 independent Fraction/divmod oracle PASS: {quant} quantization rows, "
          f"{clocks} UTC rows, 2 fresh processes, {rejected} corrupted rows rejected; "
          f"{len(first)} bytes SHA256={hashlib.sha256(first).hexdigest()}")


if __name__ == "__main__":
    main()
