import asyncio
import socket
import yaml
import os
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any
from forwarder import Forwarder
from core.data_plane import (
    DeduplicationMode,
    ProcessingSnapshot,
    ProcessorOutput,
    RoutingDisposition,
)
from core.ingress_frame import (
    coerce_ingress_frame,
    frame_from_udp_datagram,
)
from core.network_policy import NetworkPolicy, compile_ingress_policy
from core.python_data_plane import PythonDataPlaneProcessor
from core.runtime_control import build_optional_routing_control_server
from core.runtime_routing import load_optional_routing_table
from core.routing_state import RoutingState
from core.source_identity import build_udp_source_id
from aismixer_secure import secure_server

try:
    from setproctitle import setproctitle
    setproctitle('aismixer')
except ImportError:
    pass  # No effect on Windows or if not installed


def ts() -> str:
    return str(time.time())


def format_source(ip, port):
    return f"[{ip}]:{port}" if ':' in ip else f"{ip}:{port}"


def _ingress_task_name(role, index, entry, ip, port):
    configured_id = entry.get("id")
    label = (
        configured_id
        if isinstance(configured_id, str) and configured_id
        else format_source(ip, port)
    )
    safe_label = label.encode("unicode_escape").decode("ascii")[:80]
    return f"{role}-ingress:{index}:{safe_label}"


def load_config():
    config_path = "/etc/aismixer/config.yaml"
    if not os.path.exists(config_path):
        config_path = "config.yaml"
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def load_udp_alias_map(cfg) -> dict:
    """
    Зарежда IP->alias mapping.
    cfg['udp_alias_map_file'] е по желание.
    Ако липсва, се пробват ./udp_alias_map.yaml и /etc/aismixer/udp_alias_map.yaml.
    Поддържа:
      - {'udp_alias_map': [{'ip': '1.2.3.4','id':'boat'}, ...]}
      - {'1.2.3.4': 'boat', '2001:db8::1': 'v6alias'}
    """
    # 1) ясен път от конфига
    path = None
    if isinstance(cfg, dict):
        path = cfg.get('udp_alias_map_file')

    # 2) кандидати по подразбиране
    candidates = [p for p in [path, 'udp_alias_map.yaml',
                              '/etc/aismixer/udp_alias_map.yaml'] if p]
    for p in candidates:
        try:
            if os.path.exists(p):
                with open(p, 'r') as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict) and 'udp_alias_map' in data:
                    out = {}
                    for e in data.get('udp_alias_map') or []:
                        ip, aid = e.get('ip'), e.get('id')
                        if ip and aid:
                            out[str(ip)] = str(aid)
                    return out
                elif isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception:
            # тихо игнорирай повреден файл, връщай празен мап
            pass
    return {}


config = load_config()

SEC_INPUTS = config.get("sec_inputs", [])
UDP_INPUTS = config.get("udp_inputs", [])
FORWARDERS = config.get("forwarders", [])
STATION_ID = config.get("station_id", "mixstation_1")
UDP_ALIAS_MAP = load_udp_alias_map(config)
DEBUG = config.get("debug", True)
G_PRESERVE_INGRESS_GID = config.get("g_preserve_ingress_gid", True)
G_ID_DIGITS = config.get("g_id_digits", 18)
G_ALWAYS_TAG_SINGLE = config.get("g_always_tag_single", False)
C_PRESERVE_INGRESS_C = config.get("c_preserve_ingress_c", True)
forwarder = Forwarder(FORWARDERS)
initial_routing_table = load_optional_routing_table(
    config,
    forwarder.target_id_by_name,
)
routing_state = RoutingState(initial_routing_table)
data_plane_processor = PythonDataPlaneProcessor(
    station_id=STATION_ID,
    preserve_ingress_c=C_PRESERVE_INGRESS_C,
    preserve_ingress_gid=G_PRESERVE_INGRESS_GID,
    always_tag_single=G_ALWAYS_TAG_SINGLE,
    gid_digits=G_ID_DIGITS,
)


@dataclass(frozen=True, slots=True)
class _EgressBatch:
    """Private process-local handoff with a runtime-only ordering barrier."""

    outputs: tuple[ProcessorOutput, ...]
    completion: asyncio.Future


@dataclass(frozen=True, slots=True)
class _RuntimeTaskSpec:
    """Private lazy specification for one supervised runtime task."""

    name: str
    coroutine_factory: Callable[[], Coroutine[Any, Any, None]]


async def _cancel_and_await_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _supervise_named_tasks(task_specs):
    """Fail fast while owning every named task through final outcome retrieval."""

    specs = tuple(task_specs)
    if not specs:
        raise RuntimeError("Runtime supervision requires at least one task")

    tasks = []
    try:
        for spec in specs:
            coroutine = spec.coroutine_factory()
            try:
                task = asyncio.create_task(coroutine, name=spec.name)
            except BaseException:
                close = getattr(coroutine, "close", None)
                if close is not None:
                    close()
                raise
            tasks.append(task)

        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        completed_tasks = tuple(task for task in tasks if task.done())

        for task in completed_tasks:
            if task.cancelled():
                continue
            failure = task.exception()
            if failure is not None:
                raise failure

        completed_task = completed_tasks[0]
        if completed_task.cancelled():
            raise RuntimeError(
                "Essential runtime task "
                f"{completed_task.get_name()!r} was cancelled unexpectedly"
            )
        raise RuntimeError(
            "Essential runtime task "
            f"{completed_task.get_name()!r} terminated unexpectedly"
        )
    finally:
        await _cancel_and_await_tasks(tasks)


async def ingress_fan_in_loop(input_queues, output_queue):
    """Preserve queue identity while owning every private reader task."""

    async def reader(q):
        while True:
            item = await q.get()
            await output_queue.put(item)

    if not input_queues:
        await asyncio.get_running_loop().create_future()

    await _supervise_named_tasks(
        _RuntimeTaskSpec(
            name=f"ingress-reader:{index}",
            coroutine_factory=partial(reader, queue),
        )
        for index, queue in enumerate(input_queues)
    )


def _cancel_or_retrieve_completion(completion):
    if not completion.done():
        completion.cancel()
    elif not completion.cancelled():
        completion.exception()


def compile_input_policies(entries, kind):
    return tuple(
        compile_ingress_policy(entry, context=f"{kind}[{index}]")
        for index, entry in enumerate(entries)
    )


async def processor_stage_loop(
    ingress_queue,
    egress_queue,
    routing_state=None,
    processor=None,
    legacy_target_ids=(),
):
    """Process one accepted frame and await its egress completion barrier."""

    active_processor = (
        data_plane_processor
        if processor is None
        else processor
    )
    if not isinstance(legacy_target_ids, (str, bytes)):
        legacy_target_ids = tuple(legacy_target_ids)

    while True:
        item = await ingress_queue.get()
        frame = coerce_ingress_frame(item)
        if frame is None:
            continue

        if routing_state is None:
            routing_generation = 0
            routing_table = None
        else:
            routing_snapshot = routing_state.snapshot()
            routing_generation = routing_snapshot.generation
            routing_table = routing_snapshot.table

        if routing_table is None:
            processing_snapshot = ProcessingSnapshot(
                routing_generation=routing_generation,
                deduplication_mode=DeduplicationMode.GLOBAL,
                target_ids=legacy_target_ids,
            )
        else:
            processing_snapshot = ProcessingSnapshot(
                routing_generation=routing_generation,
                deduplication_mode=DeduplicationMode.PER_TARGET,
                target_ids=routing_table.match_target_ids(frame.source_id),
            )

        outputs = active_processor.process(frame, processing_snapshot)
        if not outputs:
            continue

        completion = asyncio.get_running_loop().create_future()
        batch = _EgressBatch(
            outputs=outputs,
            completion=completion,
        )
        try:
            await egress_queue.put(batch)
            await completion
        finally:
            _cancel_or_retrieve_completion(completion)


async def egress_stage_loop(
    egress_queue,
    output_forwarder,
    *,
    debug=False,
    timestamp=None,
):
    """Dispatch complete processor batches sequentially in tuple order."""

    active_timestamp = ts if timestamp is None else timestamp
    while True:
        batch = await egress_queue.get()
        try:
            for output in batch.outputs:
                if debug:
                    message = output.message
                    if message.endswith("\r\n"):
                        message = message[:-2]
                    print(f"{active_timestamp()} OUTPUT => {message}")

                if output.disposition is RoutingDisposition.LEGACY_BROADCAST:
                    await output_forwarder.send(output.message)
                elif output.disposition is RoutingDisposition.TARGETED:
                    await output_forwarder.send_to_ids(
                        output.target_ids,
                        output.message,
                    )
                else:
                    raise AssertionError(
                        "Unsupported routing disposition: "
                        f"{output.disposition!r}"
                    )
        except asyncio.CancelledError:
            if not batch.completion.done():
                batch.completion.cancel()
            raise
        except BaseException as exc:
            if not batch.completion.done():
                batch.completion.set_exception(exc)
            raise
        else:
            if not batch.completion.done():
                batch.completion.set_result(None)


async def _run_runtime_stages(
    ingress_queue,
    egress_queue,
    *,
    routing_state=None,
    processor=None,
    legacy_target_ids=(),
    output_forwarder,
    debug=False,
    timestamp=None,
):
    """Run exactly one processor stage and one egress stage as one lifecycle."""

    await _supervise_named_tasks(
        (
            _RuntimeTaskSpec(
                name="processor-stage",
                coroutine_factory=partial(
                    processor_stage_loop,
                    ingress_queue,
                    egress_queue,
                    routing_state=routing_state,
                    processor=processor,
                    legacy_target_ids=legacy_target_ids,
                ),
            ),
            _RuntimeTaskSpec(
                name="egress-stage",
                coroutine_factory=partial(
                    egress_stage_loop,
                    egress_queue,
                    output_forwarder,
                    debug=debug,
                    timestamp=timestamp,
                ),
            ),
        )
    )


async def handle_socket(
    sock,
    queue,
    fixed_alias=None,
    alias_map=None,
    ingress_policy=None,
):
    loop = asyncio.get_running_loop()
    policy = ingress_policy or NetworkPolicy.unrestricted()
    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)
        source_ip, source_port = addr[:2]
        if not policy.allows(source_ip):
            continue

        mapped_alias = alias_map.get(source_ip) if alias_map else None
        alias_for_s = fixed_alias or mapped_alias
        assembler_key = f"{source_ip}:{source_port}"
        source_id = build_udp_source_id(fixed_alias, mapped_alias, source_ip)

        frame = frame_from_udp_datagram(
            data=data,
            kind="udp",
            source_id=source_id,
            alias_for_s=alias_for_s,
            remote_ip=source_ip,
            assembler_key=assembler_key,
        )

        if DEBUG:
            source_fmt = format_source(source_ip, source_port)
            normalized_text = frame.payload.decode("utf-8")
            print(f"{ts()} INPUT {source_fmt} => {normalized_text}")

        await queue.put(frame)


async def main():
    input_queues = []
    processor_queue = asyncio.Queue()
    egress_queue = asyncio.Queue(maxsize=1)
    runtime_task_specs = []
    udp_sockets = []
    sec_input_policies = compile_input_policies(SEC_INPUTS, "sec_inputs")
    udp_input_policies = compile_input_policies(UDP_INPUTS, "udp_inputs")
    control_server = build_optional_routing_control_server(
        config,
        routing_state,
        forwarder.target_id_by_name,
    )
    control_server_started = False

    try:
        if control_server is not None:
            await control_server.start()
            control_server_started = True

        # Secure входове
        for index, (entry, ingress_policy) in enumerate(
            zip(SEC_INPUTS, sec_input_policies)
        ):
            q = asyncio.Queue()
            input_queues.append(q)
            ip = entry["listen_ip"]
            port = entry["listen_port"]
            sec_id = entry.get("id")
            print(f"{ts()} Secure listening on {format_source(ip, port)}")
            runtime_task_specs.append(
                _RuntimeTaskSpec(
                    name=_ingress_task_name(
                        "udpsec",
                        index,
                        entry,
                        ip,
                        port,
                    ),
                    coroutine_factory=partial(
                        secure_server,
                        q,
                        ip,
                        port,
                        sec_input_id=sec_id,
                        ingress_policy=ingress_policy,
                    ),
                )
            )

        # UDP входове
        for index, (entry, ingress_policy) in enumerate(
            zip(UDP_INPUTS, udp_input_policies)
        ):
            q = asyncio.Queue()
            input_queues.append(q)
            ip = entry["listen_ip"]
            port = entry["listen_port"]
            family = socket.AF_INET6 if ':' in ip else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_DGRAM)
            udp_sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((ip, port))
            sock.setblocking(False)
            print(f"{ts()} Listening on {format_source(ip, port)}")
            # ако има id -> фиксиран alias за целия вход
            fixed_alias = entry.get("id")
            runtime_task_specs.append(
                _RuntimeTaskSpec(
                    name=_ingress_task_name(
                        "udp",
                        index,
                        entry,
                        ip,
                        port,
                    ),
                    coroutine_factory=partial(
                        handle_socket,
                        sock,
                        q,
                        fixed_alias,
                        alias_map=UDP_ALIAS_MAP if not fixed_alias else None,
                        ingress_policy=ingress_policy,
                    ),
                )
            )

        # Ingress fan-in + processor + egress
        runtime_task_specs.extend(
            (
                _RuntimeTaskSpec(
                    name="ingress-fan-in",
                    coroutine_factory=partial(
                        ingress_fan_in_loop,
                        tuple(input_queues),
                        processor_queue,
                    ),
                ),
                _RuntimeTaskSpec(
                    name="processor-stage",
                    coroutine_factory=partial(
                        processor_stage_loop,
                        processor_queue,
                        egress_queue,
                        routing_state=routing_state,
                        processor=data_plane_processor,
                        legacy_target_ids=forwarder.all_target_ids,
                    ),
                ),
                _RuntimeTaskSpec(
                    name="egress-stage",
                    coroutine_factory=partial(
                        egress_stage_loop,
                        egress_queue,
                        forwarder,
                        debug=DEBUG,
                        timestamp=ts,
                    ),
                ),
            )
        )
        await _supervise_named_tasks(runtime_task_specs)
    finally:
        for sock in udp_sockets:
            sock.close()
        if control_server_started:
            await control_server.close()
        forwarder.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting.")
