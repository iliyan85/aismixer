import asyncio
import yaml
import os
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any
from forwarder import Forwarder
from core.data_plane import (
    DeduplicationMode,
    OutputBatch,
    ProcessingSnapshot,
    ProcessingWorkItem,
)
from core.ingress_frame import (
    IngressFrame,
    coerce_ingress_frame,
    frame_from_udp_datagram,
)
from core.metrics import EgressMetricsSnapshot, QueueMetricsSnapshot
from core.network_policy import NetworkPolicy, compile_ingress_policy
from core.python_data_plane import PythonDataPlaneProcessor
from core.runtime_control import (
    build_optional_routing_control_server,
    load_optional_routing_control_unix_settings,
)
from core.runtime_statistics import InputTrafficMetrics, RuntimeStatisticsProvider
from core.runtime_routing import load_optional_routing_table
from core.routing_state import RoutingState
from core.source_identity import build_udp_source_id
from core.udp_listener import create_udp_listener_socket
from core.udpsec_identity import (
    sec_inputs_require_udpsec,
    udpsec_server_identity_service,
)


DEFAULT_INGRESS_QUEUE_MAXSIZE = 1024
DEFAULT_PROCESSING_QUEUE_MAXSIZE = 1024

try:
    from setproctitle import setproctitle
    setproctitle('aismixer')
except ImportError:
    pass  # No effect on Windows or if not installed


_secure_server_impl = None


def _load_secure_server():
    """Load UDPSEC transport code only for configurations that require it."""

    global _secure_server_impl
    if _secure_server_impl is None:
        from aismixer_secure import secure_server as implementation

        _secure_server_impl = implementation
    return _secure_server_impl


async def secure_server(*args, **kwargs):
    """Run the lazily loaded UDPSEC producer."""

    implementation = _load_secure_server()
    await implementation(*args, **kwargs)


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


def create_data_plane_processor() -> PythonDataPlaneProcessor:
    """Create the processor owned by one production runtime invocation."""

    return PythonDataPlaneProcessor(
        station_id=STATION_ID,
        preserve_ingress_c=C_PRESERVE_INGRESS_C,
        preserve_ingress_gid=G_PRESERVE_INGRESS_GID,
        always_tag_single=G_ALWAYS_TAG_SINGLE,
        gid_digits=G_ID_DIGITS,
    )


@dataclass(frozen=True, slots=True)
class _EgressBatch:
    """Private process-local handoff with a runtime-only ordering barrier."""

    output_batch: OutputBatch
    completion: asyncio.Future


@dataclass(frozen=True, slots=True)
class _RuntimeTaskSpec:
    """Private lazy specification for one supervised runtime task."""

    name: str
    coroutine_factory: Callable[[], Coroutine[Any, Any, None]]


def _validate_queue_capacity(capacity, *, name):
    """Return one positive item-count queue capacity."""

    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise TypeError(f"{name} must be an integer")
    if capacity < 1:
        raise ValueError(f"{name} must be at least 1")
    return capacity


class _ObservedQueue(asyncio.Queue):
    """Bounded asyncio queue with per-instance lifetime counters."""

    def __init__(self, *, name, maxsize):
        maxsize = _validate_queue_capacity(maxsize, name="maxsize")
        initial_snapshot = QueueMetricsSnapshot(
            name=name,
            capacity=maxsize,
            depth=0,
            peak_depth=0,
            enqueued=0,
            dequeued=0,
            put_waits=0,
            current_put_waiters=0,
        )
        super().__init__(maxsize=maxsize)
        self._metrics_name = initial_snapshot.name
        self._peak_depth = 0
        self._enqueued = 0
        self._dequeued = 0
        self._put_waits = 0
        self._current_put_waiters = 0

    def put_nowait(self, item):
        """Insert immediately and count only successful insertions."""

        super().put_nowait(item)
        self._enqueued += 1
        self._peak_depth = max(self._peak_depth, self.qsize())

    async def put(self, item):
        """Insert, recording one capacity wait when initially full."""

        waited_for_capacity = self.full()
        if waited_for_capacity:
            self._put_waits += 1
            self._current_put_waiters += 1
        try:
            await super().put(item)
        finally:
            if waited_for_capacity:
                self._current_put_waiters -= 1

    def get_nowait(self):
        """Remove immediately and count only successful removals."""

        item = super().get_nowait()
        self._dequeued += 1
        return item

    def metrics_snapshot(self) -> QueueMetricsSnapshot:
        """Return fresh immutable lifetime metrics without altering the queue."""

        return QueueMetricsSnapshot(
            name=self._metrics_name,
            capacity=self.maxsize,
            depth=self.qsize(),
            peak_depth=self._peak_depth,
            enqueued=self._enqueued,
            dequeued=self._dequeued,
            put_waits=self._put_waits,
            current_put_waiters=self._current_put_waiters,
        )


class _EgressMetrics:
    """Own lifetime counters for one local egress-stage lifecycle."""

    __slots__ = (
        "_batches_started",
        "_batches_completed",
        "_batches_failed",
        "_batches_cancelled",
        "_active_batches",
        "_outputs_started",
        "_outputs_completed",
        "_outputs_failed",
        "_outputs_cancelled",
        "_active_outputs",
    )

    def __init__(self):
        self._batches_started = 0
        self._batches_completed = 0
        self._batches_failed = 0
        self._batches_cancelled = 0
        self._active_batches = 0
        self._outputs_started = 0
        self._outputs_completed = 0
        self._outputs_failed = 0
        self._outputs_cancelled = 0
        self._active_outputs = 0

    def batch_started(self):
        self._batches_started += 1
        self._active_batches += 1

    def batch_completed(self):
        self._batches_completed += 1
        self._active_batches -= 1

    def batch_failed(self):
        self._batches_failed += 1
        self._active_batches -= 1

    def batch_cancelled(self):
        self._batches_cancelled += 1
        self._active_batches -= 1

    def output_started(self):
        self._outputs_started += 1
        self._active_outputs += 1

    def output_completed(self):
        self._outputs_completed += 1
        self._active_outputs -= 1

    def output_failed(self):
        self._outputs_failed += 1
        self._active_outputs -= 1

    def output_cancelled(self):
        self._outputs_cancelled += 1
        self._active_outputs -= 1

    def metrics_snapshot(self) -> EgressMetricsSnapshot:
        """Return fresh immutable local-operation metrics."""

        return EgressMetricsSnapshot(
            batches_started=self._batches_started,
            batches_completed=self._batches_completed,
            batches_failed=self._batches_failed,
            batches_cancelled=self._batches_cancelled,
            active_batches=self._active_batches,
            outputs_started=self._outputs_started,
            outputs_completed=self._outputs_completed,
            outputs_failed=self._outputs_failed,
            outputs_cancelled=self._outputs_cancelled,
            active_outputs=self._active_outputs,
        )


class _BoundedProcessingQueue:
    """Own a bounded work queue and its matching admission permits."""

    __slots__ = (
        "_slots",
        "_work_queue",
        "_peak_depth",
        "_enqueued",
        "_dequeued",
        "_put_waits",
        "_current_put_waiters",
    )

    def __init__(self, maxsize):
        maxsize = _validate_queue_capacity(
            maxsize,
            name="processing_queue_maxsize",
        )
        self._work_queue = asyncio.Queue(maxsize=maxsize)
        self._slots = asyncio.BoundedSemaphore(maxsize)
        self._peak_depth = 0
        self._enqueued = 0
        self._dequeued = 0
        self._put_waits = 0
        self._current_put_waiters = 0

    @property
    def maxsize(self):
        return self._work_queue.maxsize

    def qsize(self):
        return self._work_queue.qsize()

    async def admit(self, work_item_factory):
        """Wait for capacity, then synchronously bind and enqueue one item."""

        waited_for_capacity = self._slots.locked()
        if waited_for_capacity:
            self._put_waits += 1
            self._current_put_waiters += 1
        try:
            await self._slots.acquire()
        finally:
            if waited_for_capacity:
                self._current_put_waiters -= 1
        try:
            work_item = work_item_factory()
            if not isinstance(work_item, ProcessingWorkItem):
                raise TypeError(
                    "work_item_factory must return a ProcessingWorkItem"
                )
            self._work_queue.put_nowait(work_item)
            self._enqueued += 1
            self._peak_depth = max(self._peak_depth, self.qsize())
        except BaseException:
            self._slots.release()
            raise

    async def get(self):
        """Dequeue one item and immediately return its queue-slot permit."""

        work_item = await self._work_queue.get()
        self._dequeued += 1
        self._slots.release()
        return work_item

    def metrics_snapshot(self) -> QueueMetricsSnapshot:
        """Return fresh immutable admission-queue lifetime metrics."""

        return QueueMetricsSnapshot(
            name="processing",
            capacity=self.maxsize,
            depth=self.qsize(),
            peak_depth=self._peak_depth,
            enqueued=self._enqueued,
            dequeued=self._dequeued,
            put_waits=self._put_waits,
            current_put_waiters=self._current_put_waiters,
        )


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


def _bind_processing_work_item(
    frame,
    *,
    routing_state=None,
    legacy_target_ids,
):
    """Bind one accepted frame to its target-only routing snapshot."""

    if not isinstance(frame, IngressFrame):
        raise TypeError("frame must be an IngressFrame")

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

    return ProcessingWorkItem(
        frame=frame,
        snapshot=processing_snapshot,
    )


async def ingress_fan_in_loop(
    input_queues,
    processing_queue,
    *,
    routing_state=None,
    legacy_target_ids,
):
    """Bind accepted ingress items while owning every private reader task.

    Each reader may hold one accepted frame while awaiting the shared
    processing capacity. Separate input queues isolate private backlogs but
    do not imply fair admission among readers.
    """

    if isinstance(legacy_target_ids, (str, bytes)):
        raise TypeError(
            "legacy_target_ids must be a non-string iterable"
        )
    legacy_target_ids = tuple(legacy_target_ids)

    async def reader(q):
        while True:
            item = await q.get()
            frame = coerce_ingress_frame(item)
            if frame is None:
                continue
            await processing_queue.admit(
                partial(
                    _bind_processing_work_item,
                    frame,
                    routing_state=routing_state,
                    legacy_target_ids=legacy_target_ids,
                )
            )

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
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        raise ValueError(f"{kind} must be a list of ingress mappings")

    for index, entry in enumerate(entries):
        context = f"{kind}[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{context} must be a mapping")
        listen_ip = entry.get("listen_ip")
        if not isinstance(listen_ip, str):
            raise ValueError(f"{context}.listen_ip must be a string")
        listen_port = entry.get("listen_port")
        if (
            isinstance(listen_port, bool)
            or not isinstance(listen_port, int)
            or not 0 <= listen_port <= 65535
        ):
            raise ValueError(
                f"{context}.listen_port must be an integer from 0 to 65535"
            )

    return tuple(
        compile_ingress_policy(entry, context=f"{kind}[{index}]")
        for index, entry in enumerate(entries)
    )


def prepare_udpsec_ingress_activation(
    sec_inputs,
    *,
    identity_service=None,
):
    """Ensure and load identity before a UDPSEC configuration is activated."""

    service = (
        udpsec_server_identity_service
        if identity_service is None
        else identity_service
    )
    if not sec_inputs_require_udpsec(sec_inputs):
        return None

    _load_secure_server()
    return service.ensure_for_sec_inputs(sec_inputs)


async def processor_stage_loop(
    processing_queue,
    egress_queue,
    *,
    processor,
):
    """Process one bound work item and await its egress completion barrier."""

    while True:
        work_item = await processing_queue.get()
        if not isinstance(work_item, ProcessingWorkItem):
            raise TypeError(
                "processor queue item must be a ProcessingWorkItem"
            )

        output_batch = processor.process(
            work_item.frame,
            work_item.snapshot,
        )
        if not output_batch.outputs:
            continue

        completion = asyncio.get_running_loop().create_future()
        batch = _EgressBatch(
            output_batch=output_batch,
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
    metrics=None,
):
    """Dispatch complete processor batches sequentially in output order."""

    active_timestamp = ts if timestamp is None else timestamp
    active_metrics = _EgressMetrics() if metrics is None else metrics
    while True:
        batch = await egress_queue.get()
        active_metrics.batch_started()
        try:
            for output in batch.output_batch.outputs:
                active_metrics.output_started()
                try:
                    if debug:
                        message = output.message
                        if message.endswith(b"\r\n"):
                            message = message[:-2]
                        display_message = message.decode(
                            "utf-8",
                            errors="replace",
                        )
                        print(
                            f"{active_timestamp()} OUTPUT => {display_message}"
                        )

                    await output_forwarder.send_to_ids(
                        output.target_ids,
                        output.message,
                    )
                except asyncio.CancelledError:
                    active_metrics.output_cancelled()
                    raise
                except BaseException:
                    active_metrics.output_failed()
                    raise
                else:
                    active_metrics.output_completed()
        except asyncio.CancelledError:
            active_metrics.batch_cancelled()
            if not batch.completion.done():
                batch.completion.cancel()
            raise
        except BaseException as exc:
            active_metrics.batch_failed()
            if not batch.completion.done():
                batch.completion.set_exception(exc)
            raise
        else:
            active_metrics.batch_completed()
            if not batch.completion.done():
                batch.completion.set_result(None)


async def _run_runtime_stages(
    ingress_queue,
    egress_queue,
    *,
    routing_state=None,
    processor,
    legacy_target_ids,
    output_forwarder,
    debug=False,
    timestamp=None,
    processing_queue_maxsize=DEFAULT_PROCESSING_QUEUE_MAXSIZE,
    egress_metrics=None,
):
    """Run one ingress binding, processor, and egress lifecycle."""

    processing_queue = _BoundedProcessingQueue(processing_queue_maxsize)
    active_egress_metrics = (
        _EgressMetrics() if egress_metrics is None else egress_metrics
    )

    await _supervise_named_tasks(
        (
            _RuntimeTaskSpec(
                name="ingress-fan-in",
                coroutine_factory=partial(
                    ingress_fan_in_loop,
                    (ingress_queue,),
                    processing_queue,
                    routing_state=routing_state,
                    legacy_target_ids=legacy_target_ids,
                ),
            ),
            _RuntimeTaskSpec(
                name="processor-stage",
                coroutine_factory=partial(
                    processor_stage_loop,
                    processing_queue,
                    egress_queue,
                    processor=processor,
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
                    metrics=active_egress_metrics,
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
    *,
    input_traffic=None,
):
    loop = asyncio.get_running_loop()
    policy = ingress_policy or NetworkPolicy.unrestricted()
    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)
        if input_traffic is not None:
            input_traffic.transport_received(data)
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
        if input_traffic is not None:
            input_traffic.frame_accepted(frame.payload)


async def main(
    *,
    ingress_queue_maxsize=DEFAULT_INGRESS_QUEUE_MAXSIZE,
    processing_queue_maxsize=DEFAULT_PROCESSING_QUEUE_MAXSIZE,
):
    ingress_queue_maxsize = _validate_queue_capacity(
        ingress_queue_maxsize,
        name="ingress_queue_maxsize",
    )
    processing_queue_maxsize = _validate_queue_capacity(
        processing_queue_maxsize,
        name="processing_queue_maxsize",
    )
    sec_input_policies = compile_input_policies(SEC_INPUTS, "sec_inputs")
    udp_input_policies = compile_input_policies(UDP_INPUTS, "udp_inputs")
    load_optional_routing_control_unix_settings(config)
    udpsec_identity = prepare_udpsec_ingress_activation(SEC_INPUTS)
    if udpsec_identity is not None and udpsec_identity.generated:
        print(
            "[+] Generated UDPSEC server identity: "
            f"{udpsec_identity.public_path}"
        )

    processor_queue = _BoundedProcessingQueue(processing_queue_maxsize)
    processor = create_data_plane_processor()
    input_queues = []
    input_traffic = []
    egress_queue = _ObservedQueue(name="egress", maxsize=1)
    egress_metrics = _EgressMetrics()
    runtime_task_specs = []
    udp_sockets = []
    control_server = None
    control_server_started = False

    try:
        # Secure входове
        for index, (entry, ingress_policy) in enumerate(
            zip(SEC_INPUTS, sec_input_policies)
        ):
            ip = entry["listen_ip"]
            port = entry["listen_port"]
            task_name = _ingress_task_name(
                "udpsec",
                index,
                entry,
                ip,
                port,
            )
            q = _ObservedQueue(
                name=task_name,
                maxsize=ingress_queue_maxsize,
            )
            input_queues.append(q)
            traffic = InputTrafficMetrics(task_name, "udpsec")
            input_traffic.append(traffic)
            sec_id = entry.get("id")
            print(f"{ts()} Secure listening on {format_source(ip, port)}")
            runtime_task_specs.append(
                _RuntimeTaskSpec(
                    name=task_name,
                    coroutine_factory=partial(
                        secure_server,
                        q,
                        ip,
                        port,
                        sec_input_id=sec_id,
                        ingress_policy=ingress_policy,
                        input_traffic=traffic,
                        server_private_key=udpsec_identity.private_key,
                    ),
                )
            )

        # UDP входове
        for index, (entry, ingress_policy) in enumerate(
            zip(UDP_INPUTS, udp_input_policies)
        ):
            ip = entry["listen_ip"]
            port = entry["listen_port"]
            task_name = _ingress_task_name(
                "udp",
                index,
                entry,
                ip,
                port,
            )
            q = _ObservedQueue(
                name=task_name,
                maxsize=ingress_queue_maxsize,
            )
            input_queues.append(q)
            traffic = InputTrafficMetrics(task_name, "udp")
            input_traffic.append(traffic)
            sock = create_udp_listener_socket(ip, reuse_address=True)
            udp_sockets.append(sock)
            sock.bind((ip, port))
            sock.setblocking(False)
            print(f"{ts()} Listening on {format_source(ip, port)}")
            # ако има id -> фиксиран alias за целия вход
            fixed_alias = entry.get("id")
            runtime_task_specs.append(
                _RuntimeTaskSpec(
                    name=task_name,
                    coroutine_factory=partial(
                        handle_socket,
                        sock,
                        q,
                        fixed_alias,
                        alias_map=UDP_ALIAS_MAP if not fixed_alias else None,
                        ingress_policy=ingress_policy,
                        input_traffic=traffic,
                    ),
                )
            )

        ingress_queues = tuple(input_queues)
        statistics_provider = RuntimeStatisticsProvider(
            ingress_queues=ingress_queues,
            processing_queue=processor_queue,
            processor=processor,
            egress_queue=egress_queue,
            egress_operations=egress_metrics,
            input_traffic=input_traffic,
            output_traffic=forwarder,
        )
        control_server = build_optional_routing_control_server(
            config,
            routing_state,
            forwarder.target_id_by_name,
            statistics_provider,
        )
        if control_server is not None:
            await control_server.start()
            control_server_started = True

        # Ingress fan-in + processor + egress
        runtime_task_specs.extend(
            (
                _RuntimeTaskSpec(
                    name="ingress-fan-in",
                    coroutine_factory=partial(
                        ingress_fan_in_loop,
                        ingress_queues,
                        processor_queue,
                        routing_state=routing_state,
                        legacy_target_ids=forwarder.all_target_ids,
                    ),
                ),
                _RuntimeTaskSpec(
                    name="processor-stage",
                    coroutine_factory=partial(
                        processor_stage_loop,
                        processor_queue,
                        egress_queue,
                        processor=processor,
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
                        metrics=egress_metrics,
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
