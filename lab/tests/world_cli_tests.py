"""Independent stdlib-only W1 trace, schedule, Philox and ellipsoid oracle.

All receivers/routes here are unqualified synthetic examples. This test makes
no AIS protocol, RF reception, site suitability or coastline assertion.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

UNITS = 9_830_400
DENOMINATOR = 900 * UNITS
BANNER = b"WORLD STATE / LIFECYCLE ONLY -- NO AIS TDMA OR RF RECEPTION SIMULATED"
MASK = (1 << 64) - 1
WAYPOINTS = {1: (28.53, 43.69), 2: (28.53, 43.54), 3: (28.34, 43.44),
             4: (28.17, 43.41), 5: (27.91, 43.21), 6: (27.47, 42.50),
             7: (28.96, 41.01), 8: (39.72, 41.01), 9: (41.64, 41.65),
             10: (39.72, 43.59), 11: (37.77, 44.72), 12: (33.52, 44.61),
             13: (30.73, 46.48), 14: (29.65, 45.16), 15: (28.65, 44.18), 16: (28.58, 43.82)}
MODELS = {
    101: dict(equipment=201, radio=301, initial=[5], first=[4], cycling=[1, 4, 5, 6],
              speed=(10000, 20000), speed_hold=(100, 200), status_weights=(6, 2, 2),
              status_hold=(100, 200), cap=4000, mount=18000, reach=(10000000, 60000000), reach_hold=(100, 200)),
    102: dict(equipment=202, radio=302, initial=[6, 7], first=[8, 9], cycling=list(range(1, 17)),
              speed=(12000, 18000), speed_hold=(100, 200), status_weights=(8, 1, 1),
              status_hold=(100, 200), cap=3000, mount=15000, reach=(20000000, 80000000), reach_hold=(110, 210))}
REGIONS = {11: dict(center=(27.91, 43.21), radius=20000, count=(1, 10), elevation=(10000, 300000)),
           12: dict(center=(28.17, 43.41), radius=10000, count=(1, 5), elevation=(10000, 200000))}
KIND_ORDER = {"waypoint_arrival": 0, "speed_changed": 1, "navigation_changed": 2,
              "reach_changed": 3, "initial_network_entry_preparation": 4,
              "initial_receiver_power_delay": 4, "equipment_on": 5, "equipment_off": 6}


def require(value, reason):
    if not value:
        raise AssertionError(reason)


def fnv(data, state=14695981039346656037):
    for byte in data:
        state = ((state ^ byte) * 1099511628211) & MASK
    return state


def word(seed, entity, purpose, episode, draw=0):
    """Independent Random123 Philox4x32-10 and documented W1 adapter."""
    a, b, c, d = episode & 0xFFFFFFFF, episode >> 32, draw // 4, (1 << 24) | purpose
    key = seed ^ entity
    x, y = key & 0xFFFFFFFF, key >> 32
    for _ in range(10):
        p, q = 0xD2511F53 * a, 0xCD9E8D57 * c
        a, b, c, d = ((q >> 32) ^ b ^ x) & 0xFFFFFFFF, q & 0xFFFFFFFF, ((p >> 32) ^ d ^ y) & 0xFFFFFFFF, p & 0xFFFFFFFF
        x, y = (x + 0x9E3779B9) & 0xFFFFFFFF, (y + 0xBB67AE85) & 0xFFFFFFFF
    return (a, b, c, d)[draw % 4]


def bounded(seed, entity, purpose, episode, bounds):
    low, high = bounds
    width = high - low + 1
    threshold = ((-width) & MASK) % width
    for proposal in range(32):
        value = (word(seed, entity, purpose, episode, 2 * proposal) << 32) | word(seed, entity, purpose, episode, 2 * proposal + 1)
        if value >= threshold:
            return low + value % width
    raise AssertionError("independent bounded Philox exhausted")


def power(seed, key, seconds):
    deadline = bounded(seed, key, 8, 0, (0, 10))
    if seconds < deadline:
        return False, 0
    episode = 0
    while True:
        deadline += bounded(seed, key, 9, episode, (100, 1000))
        if seconds < deadline:
            return True, episode + 1
        deadline += bounded(seed, key, 10, episode, (10, 300))
        if seconds < deadline:
            return False, episode + 1
        episode += 1


def episodes(seed, entity, value_purpose, hold_purpose, values, holds, duration):
    result, start, episode = [], 0, 0
    while start <= duration:
        value = bounded(seed, entity, value_purpose, episode, values)
        end = start + bounded(seed, entity, hold_purpose, episode, holds)
        result.append((start, end, value, episode))
        start, episode = end, episode + 1
    return result


def current(schedule, second):
    return next(row for row in schedule if row[0] <= second < row[1])


def derivative(state):
    latitude, _, azimuth = state
    e2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
    denominator = 1 - e2 * math.sin(latitude) ** 2
    n = 6378137 / math.sqrt(denominator)
    m = 6378137 * (1 - e2) / denominator ** 1.5
    return math.cos(azimuth) / m, math.sin(azimuth) / (n * math.cos(latitude)), math.sin(azimuth) * math.tan(latitude) / n


def direct(origin, bearing, distance, step=500):
    """Independent ellipsoid ODE RK4, not Vincenty or a spherical fallback."""
    state = [math.radians(origin[1]), math.radians(origin[0]), bearing]
    count = max(1, math.ceil(distance / step))
    h = distance / count
    for _ in range(count):
        a = derivative(state)
        b = derivative([v + h * d / 2 for v, d in zip(state, a)])
        c = derivative([v + h * d / 2 for v, d in zip(state, b)])
        d = derivative([v + h * q for v, q in zip(state, c)])
        state = [v + h * (w + 2*x + 2*y + z) / 6 for v, w, x, y, z in zip(state, a, b, c, d)]
    return state


INVERSE = {}


def inverse(origin, target):
    key = origin, target
    if key in INVERSE:
        return INVERSE[key]
    latitude1, latitude2 = math.radians(origin[1]), math.radians(target[1])
    delta = math.radians(target[0] - origin[0])
    bearing = math.atan2(math.sin(delta) * math.cos(latitude2), math.cos(latitude1) * math.sin(latitude2) - math.sin(latitude1) * math.cos(latitude2) * math.cos(delta))
    distance = 6371000 * math.acos(max(-1, min(1, math.sin(latitude1)*math.sin(latitude2) + math.cos(latitude1)*math.cos(latitude2)*math.cos(delta))))
    for _ in range(8):
        value = direct(origin, bearing, distance)
        residual = value[0] - latitude2, value[1] - math.radians(target[0])
        if max(map(abs, residual)) < 2e-15:
            break
        da, dd = 1e-5, 1.0
        ap, am = direct(origin, bearing + da, distance), direct(origin, bearing - da, distance)
        dp, dm = direct(origin, bearing, distance + dd), direct(origin, bearing, distance - dd)
        j00, j10 = (ap[0]-am[0])/(2*da), (ap[1]-am[1])/(2*da)
        j01, j11 = (dp[0]-dm[0])/(2*dd), (dp[1]-dm[1])/(2*dd)
        determinant = j00*j11 - j01*j10
        bearing -= (j11*residual[0] - j01*residual[1]) / determinant
        distance -= (-j10*residual[0] + j00*residual[1]) / determinant
    else:
        raise AssertionError("independent ellipsoid shooting did not converge")
    INVERSE[key] = distance, bearing
    return distance, bearing


def error_m(longitude, latitude, reference):
    return 6378137 * math.hypot(math.radians(latitude)-reference[0], (math.radians(longitude)-reference[1])*math.cos(reference[0]))


def run(exe, *arguments, success=True, hidden=False):
    env = os.environ.copy()
    if hidden:
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    result = subprocess.run([str(exe), *map(str, arguments)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=120)
    require((result.returncode == 0) == success,
            f"W1 CLI exit {arguments}: code={result.returncode}, stderr={result.stderr[-2000:]!r}")
    return result


def parse_trace(raw):
    require(0 < len(raw) < 32*1024*1024 and b"\r" not in raw, "bounded LF-only canonical trace")
    lines = raw.splitlines(keepends=True)
    require(all(line.endswith(b"\n") for line in lines), "complete NDJSON line framing")
    rows = [json.loads(line) for line in lines]
    require(rows[0]["type"] == "manifest" and rows[-1]["type"] == "complete", "trace framing records")
    require(rows[-1]["status"] == "COMPLETE_WORLD_ONLY" and rows[-1]["records"] == len(rows)-1,
            "world completion/counts")
    digest = fnv(b"".join(lines[:-1]))
    require(rows[-1]["logical_fnv1a64"] == f"{digest:016x}", "independent canonical-byte FNV")
    require(set(row["type"] for row in rows) <= {"manifest", "transition", "vessel", "receiver", "complete"},
            "radio/NMEA/TDMA records escaped world-only trace")
    return rows


def verify_trace(raw, source_dir, expected_seed=42, duration=18000):
    rows = parse_trace(raw)
    manifest = rows[0]
    require((manifest["trace_schema"], manifest["world_schema"], manifest["rng_version"], manifest["model_version"],
             manifest["time_units_per_second"], manifest["effective_seed"], manifest["duration_units"], manifest["step_units"]) ==
            (1, 1, 1, 1, UNITS, expected_seed, duration*UNITS, 60*UNITS), "versioned canonical manifest")
    require(manifest["geodesic_version"] == "wgs84-vincenty-bounded-v1" and BANNER.decode() in manifest["scope"], "geodesy/scope qualification")
    for field, name in (("scenario_fnv1a64", "black_sea_world_v1.yaml"), ("waypoint_fnv1a64", "black_sea_waypoints_v1.yaml"), ("regions_fnv1a64", "black_sea_regions_v1.yaml")):
        source = (source_dir / "world" / "profiles" / name).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        require(manifest[field] == f"{fnv(source):016x}", "independent source provenance fingerprint")
    schedules = {}
    for identity, model in MODELS.items():
        schedules[identity] = (
            episodes(expected_seed, identity, 4, 5, model["speed"], model["speed_hold"], duration),
            episodes(expected_seed, identity, 6, 7, (0, sum(model["status_weights"])-1), model["status_hold"], duration),
            episodes(expected_seed, model["equipment"], 15, 16, model["reach"], model["reach_hold"], duration))
    receiver_count = {identity: bounded(expected_seed, identity, 11, 0, model["count"]) for identity, model in REGIONS.items()}
    snapshots, receiver_rows, transitions = {}, {}, []
    last_time = -1
    last_transition = None
    for row in rows[1:-1]:
        require(row["time_units"] >= last_time, "canonical world trace regressed simulation time")
        last_time = row["time_units"]
        if row["type"] == "transition":
            require(row["kind"] in KIND_ORDER, "unknown world transition")
            key = row["time_units"], KIND_ORDER[row["kind"]], row["entity_id"], row["episode"]
            require(last_transition is None or key > last_transition, "transition causal precedence/tie order")
            last_transition = key
            transitions.append(row)
            if row["kind"] == "equipment_on":
                require(row["protocol_initialization_required"] == (row["entity_id"] <= 0xFFFFFFFF), "passive radio requested AIS protocol entry or ship skipped it")
            if row["kind"] == "equipment_off":
                require(row["abort_if_overlapping_burst"] == (row["entity_id"] <= 0xFFFFFFFF), "power cut/partial burst boundary contract")
        elif row["type"] == "vessel":
            snapshots.setdefault(row["vessel_id"], []).append(row)
        else:
            receiver_rows.setdefault((row["region_id"], row["ordinal"]), []).append(row)
    expected_times = list(range(0, duration+1, 60))
    if expected_times[-1] != duration:
        expected_times.append(duration)
    require(set(snapshots) == set(MODELS), "default world vessel catalogue")
    max_error = 0.0
    for identity, values in snapshots.items():
        model = MODELS[identity]
        speeds, statuses, reaches = schedules[identity]
        require([row["time_units"] for row in values] == [second*UNITS for second in expected_times], "sample cadence changed with compute chunking")
        route = [(model["initial"][bounded(expected_seed,identity,1,0,(0,len(model["initial"])-1))], None)]
        route[0] = (route[0][0], [p for p in model["first"] if p != route[0][0]][bounded(expected_seed,identity,2,0,(0,len([p for p in model["first"] if p != route[0][0]])-1))])
        cut_times = sorted({0, duration, *[s[0] for s in speeds if s[0] <= duration], *[s[0] for s in statuses if s[0] <= duration]})
        distance_prefix, numerator = [], 0
        for begin, end in zip(cut_times, cut_times[1:]):
            nominal = current(speeds, begin)[2]
            choice = current(statuses, begin)[2]
            status = 0 if choice < model["status_weights"][0] else 1 if choice < sum(model["status_weights"][:2]) else 3
            speed = 0 if status == 1 else min(nominal,model["cap"]) if status == 3 else nominal
            distance_prefix.append((begin,end,numerator,speed))
            numerator += (end-begin)*UNITS*speed*463
        for index, row in enumerate(values):
            second = row["time_units"] // UNITS
            speed_episode, status_episode, reach_episode = current(speeds,second), current(statuses,second), current(reaches,second)
            nominal = speed_episode[2]
            choice = status_episode[2]
            status = 0 if choice < model["status_weights"][0] else 1 if choice < sum(model["status_weights"][:2]) else 3
            speed = 0 if status == 1 else min(nominal,model["cap"]) if status == 3 else nominal
            powered, generation = power(expected_seed,model["equipment"],second)
            require((row["nominal_speed_mkn"],row["sog_mkn"],row["navigation_status"],row["equipment_on"],row["power_generation"]) ==
                    (nominal,speed,status,powered,generation), "independent navigation/power schedule mismatch")
            require(row["tx_enabled"] == powered == row["rx_enabled"], "ClassA TX/RX power uncoupled")
            require(row["antenna_offset_mm"] == model["mount"] and row["antenna_ellipsoid_mm"] is None,
                    "fixed mounting/unresolved waterline datum changed")
            require(row["effective_reach_radius_mm"] == reach_episode[2] and row["environment_revision"] == reach_episode[3]+1,
                    "independent environment schedule mismatch")
            require((row["cog_ndeg"] is None) == (speed == 0), "anchored/underway course validity")
            if second == duration:
                expected_numerator = numerator
            else:
                begin,end,prefix,effective = next(item for item in distance_prefix if item[0] <= second < item[1])
                expected_numerator = prefix + (second-begin)*UNITS*effective*463
            require(row["distance_denominator"] == DENOMINATOR and
                    (row["total_distance_mm"],row["total_distance_remainder"]) == divmod(expected_numerator,DENOMINATOR),
                    "piecewise speed integration changed under episodes/chunks")
            while len(route) <= row["leg_index"]:
                origin = route[-1][1]
                pool = [point for point in model["cycling"] if point != origin]
                route.append((origin,pool[bounded(expected_seed,identity,3,len(route),(0,len(pool)-1))]))
            require((row["leg_from"],row["destination"]) == route[row["leg_index"]], "independent waypoint selection mismatch")
            require("unqualified" in row["route_qualification"], "route presented as navigation/coastline truth")
            if index % 15 == 0 or index == len(values)-1:
                origin, destination = WAYPOINTS[row["leg_from"]], WAYPOINTS[row["destination"]]
                length,bearing = inverse(origin,destination)
                require(abs(row["leg_length_mm"]-length*1000) < 1, "independent ellipsoid leg length")
                travel = (row["leg_distance_mm"] + row["distance_remainder"] / DENOMINATOR) / 1000
                expected = direct(origin,bearing,travel)
                error = error_m(row["longitude_ndeg"]/1e9,row["latitude_ndeg"]/1e9,expected)
                max_error = max(max_error,error)
                require(error < 0.002, "independent ellipsoid world position exceeds2mm")
    require(any(not row["equipment_on"] and row["sog_mkn"] > 0 for values in snapshots.values() for row in values), "powered-OFF navigation unexercised")
    require(set(receiver_rows) == {(region, ordinal) for region,count in receiver_count.items() for ordinal in range(1,count+1)}, "stable region counts/ordinals")
    for (identity,ordinal), values in receiver_rows.items():
        model = REGIONS[identity]
        radial = (word(expected_seed,identity,12,ordinal)+0.5) / 2**32
        angle = (word(expected_seed,identity,13,ordinal)+0.5) / 2**32 * 2*math.pi
        expected = direct(model["center"],angle,model["radius"]*math.sqrt(radial))
        elevation = bounded(expected_seed,identity,14,ordinal,model["elevation"])
        require([row["time_units"] for row in values] == [second*UNITS for second in expected_times], "receiver frame continuity")
        for row in values:
            error = error_m(row["longitude_ndeg"]/1e9,row["latitude_ndeg"]/1e9,expected)
            max_error = max(max_error,error)
            require(error < 0.002 and row["antenna_elevation_mm"] == elevation and row["height_datum"] == "AMSL",
                    "area-uniform geodesic sample/elevation/datum mismatch")
            powered,generation = power(expected_seed,(identity<<32)|ordinal,row["time_units"]//UNITS)
            require(row["equipment_key"] == (identity<<32)|ordinal and row["rx_enabled"] == powered and not row["tx_enabled"] and
                    row["power_generation"] == generation, "passive independent power/identity mismatch")
            require(row["site_qualification"] == "unqualified synthetic candidate" and row["provenance"], "synthetic receiver misrepresented as measured site")
    return rows, max_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--other-exe", type=Path)
    parser.add_argument("--mode", choices=("host","gpu"), required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    profile = args.source_dir / "world" / "profiles" / "black_sea_world_v1.yaml"
    args.work_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="w1-cli-",dir=args.work_dir) as folder:
        temporary = Path(folder)
        help_result = run(args.exe,"--help",hidden=True)
        require(BANNER in help_result.stdout, "world-only help scope")
        validated = run(args.exe,"--world",profile,"--validate",hidden=True)
        require(json.loads(validated.stdout)["status"] == "VALID_WORLD_CONFIGURATION" and BANNER in validated.stderr, "CUDA-hidden validation")
        for extra in (("--seed","-1"),("--seed","01"),("--step-seconds","0"),("--block-size","128"),
                      ("--batch-size","65537"),("--chunk-seconds","0"),("--unknown",),
                      ("--world",str(profile)),("--validate","--verbose")):
            rejected = run(args.exe,"--world",profile,*extra,success=False,hidden=True)
            require(rejected.stdout == b"", "invalid CLI input emitted partial world trace")
        hidden = run(args.exe,"--world",profile,"--duration-seconds","1",success=False,hidden=True)
        require(hidden.stdout == b"" and b"CUDA" in hidden.stderr, "production operator silently used CPU fallback")
        if args.mode == "host":
            print("PASS W1 host CLI: strict options/validation, actual CUDA-hidden production rejection; no GPU claim")
            return
        baseline = run(args.exe,"--world",profile,"--seed","42","--duration-seconds","18000",
                       "--step-seconds","60","--chunk-seconds","300","--batch-size","1024","--block-size","256")
        rows,error = verify_trace(baseline.stdout,args.source_dir)
        require(BANNER in baseline.stderr and b"position_queries=" in baseline.stderr and b"device_owned_bytes=" in baseline.stderr,
                "production CUDA metrics/scope missing")
        # Qualified only after the independent schedules, geodesic shooting,
        # area sampling and byte-level FNV checks above. Keep custom profiles
        # and other toolchains outside this device-specific numeric pin.
        qualified_inputs = (rows[0]["scenario_fnv1a64"], rows[0]["waypoint_fnv1a64"], rows[0]["regions_fnv1a64"]) == (
            "c6208d82cfe68d40", "cece58237503ab93", "809b2625bb9d7cb5")
        require(qualified_inputs, "shipped W1 profile fingerprints changed; review source data and qualification explicitly")
        qualified_device = (b"gpu=NVIDIA GeForce GTX 1050 Ti sm=6.1 runtime=12090 " in baseline.stderr and
                            b"build=MSVC_19.44.35229.0_CUDA_12.9.86_sm61_fmad_false" in baseline.stderr)
        if qualified_inputs and qualified_device:
            require(rows[-1]["logical_fnv1a64"] == "6328099c89f9a685" and len(rows)-1 == 5319,
                    "qualified W1 five-hour canonical golden changed")
        else:
            print("W1 qualified numeric golden SKIPPED: non-reference GPU/toolchain; independent semantic checks continue")
        alternative = run(args.exe,"--world",profile,"--seed","42","--duration-seconds","18000",
                          "--step-seconds","60","--chunk-seconds","7","--batch-size","1","--block-size","64","--verbose")
        require(alternative.stdout == baseline.stdout, "five-hour canonical trace changed with chunk/batch/block/diagnostics")
        if args.other_exe:
            independent = run(args.other_exe,"--world",profile,"--seed","42","--duration-seconds","18000","--step-seconds","60")
            require(independent.stdout == baseline.stdout, "independent build canonical bytes differ")
        fresh = run(args.exe,"--world",profile,"--seed","fresh","--duration-seconds","120")
        effective = parse_trace(fresh.stdout)[0]["effective_seed"]
        replay = run(args.exe,"--world",profile,"--seed",effective,"--duration-seconds","120")
        require(replay.stdout == fresh.stdout, "fresh effective seed did not replay exact world trace")
        unicode_dir = temporary / "свят 海 profiles"
        shutil.copytree(profile.parent,unicode_dir)
        short = run(args.exe,"--world",profile,"--seed","42","--duration-seconds","120")
        unicode_input = run(args.exe,"--world",unicode_dir/profile.name,"--seed","42","--duration-seconds","120")
        require(unicode_input.stdout == short.stdout, "UTF8 operator input path changed canonical world state")
        destination = temporary / "изход 海.ndjson"
        exported = run(args.exe,"--world",profile,"--seed","42","--duration-seconds","120","--output",destination)
        require(exported.stdout == b"" and destination.read_bytes() == short.stdout, "Unicode named output differs from stdout trace")
        before = hashlib.sha256(destination.read_bytes()).digest()
        run(args.exe,"--world",profile,"--output",destination,success=False)
        require(hashlib.sha256(destination.read_bytes()).digest() == before, "existing world trace was overwritten")
        read_fd,write_fd = os.pipe()
        os.close(read_fd)
        try:
            child = subprocess.Popen([str(args.exe),"--world",str(profile),"--duration-seconds","1"],stdout=write_fd,stderr=subprocess.PIPE)
        finally:
            os.close(write_fd)
        _,diagnostics = child.communicate(timeout=60)
        require(child.returncode != 0 and diagnostics, "closed stdout falsely reported complete world output")
        print(f"PASS W1 GPU CLI: five hours, {len(rows)-1} canonical records, digest={rows[-1]['logical_fnv1a64']}, independent WGS84 max_error_m={error:.9f}; blocks64/256,batch1/1024,chunk7/300,Unicode,no-clobber,seed replay")


if __name__ == "__main__":
    main()
