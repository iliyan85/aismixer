import asyncio
import socket
import yaml
import os
import time
from forwarder import Forwarder
from core.data_plane import ProcessingSnapshot, RoutingDisposition
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
initial_routing_table = load_optional_routing_table(config, forwarder.target_ids)
routing_state = RoutingState(initial_routing_table)
data_plane_processor = PythonDataPlaneProcessor(
    station_id=STATION_ID,
    preserve_ingress_c=C_PRESERVE_INGRESS_C,
    preserve_ingress_gid=G_PRESERVE_INGRESS_GID,
    always_tag_single=G_ALWAYS_TAG_SINGLE,
    gid_digits=G_ID_DIGITS,
)


async def mixer_loop(input_queues, output_queue):
    async def reader(q):
        while True:
            item = await q.get()
            await output_queue.put(item)
    tasks = [asyncio.create_task(reader(q)) for q in input_queues]
    await asyncio.gather(*tasks)


async def _cancel_and_await_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def compile_input_policies(entries, kind):
    return tuple(
        compile_ingress_policy(entry, context=f"{kind}[{index}]")
        for index, entry in enumerate(entries)
    )


async def forward_loop(queue, routing_state=None, processor=None):
    """Coerce, snapshot, process, and sequentially dispatch ingress frames."""

    active_processor = (
        data_plane_processor
        if processor is None
        else processor
    )
    while True:
        item = await queue.get()
        frame = coerce_ingress_frame(item)
        if frame is None:
            continue

        if routing_state is None:
            processing_snapshot = ProcessingSnapshot(
                routing_generation=0,
                routing_table=None,
            )
        else:
            processing_snapshot = ProcessingSnapshot.from_routing_snapshot(
                routing_state.snapshot()
            )

        outputs = active_processor.process(frame, processing_snapshot)
        for output in outputs:
            if DEBUG:
                message = output.message
                if message.endswith("\r\n"):
                    message = message[:-2]
                print(f"{ts()} OUTPUT => {message}")

            if output.disposition is RoutingDisposition.LEGACY_BROADCAST:
                await forwarder.send(output.message)
            elif output.disposition is RoutingDisposition.TARGETED:
                await forwarder.send_to(
                    output.target_ids,
                    output.message,
                )
            else:
                raise AssertionError(
                    f"Unsupported routing disposition: {output.disposition!r}"
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
    mixer_queue = asyncio.Queue()
    runtime_tasks = []
    udp_sockets = []
    sec_input_policies = compile_input_policies(SEC_INPUTS, "sec_inputs")
    udp_input_policies = compile_input_policies(UDP_INPUTS, "udp_inputs")
    control_server = build_optional_routing_control_server(
        config,
        routing_state,
        forwarder.target_ids,
    )
    control_server_started = False

    try:
        if control_server is not None:
            await control_server.start()
            control_server_started = True

        # Secure входове
        for entry, ingress_policy in zip(SEC_INPUTS, sec_input_policies):
            q = asyncio.Queue()
            input_queues.append(q)
            ip = entry["listen_ip"]
            port = entry["listen_port"]
            sec_id = entry.get("id")
            print(f"{ts()} Secure listening on {format_source(ip, port)}")
            runtime_tasks.append(
                asyncio.create_task(
                    secure_server(
                        q,
                        ip,
                        port,
                        sec_input_id=sec_id,
                        ingress_policy=ingress_policy,
                    )
                )
            )

        # UDP входове
        for entry, ingress_policy in zip(UDP_INPUTS, udp_input_policies):
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
            runtime_tasks.append(
                asyncio.create_task(
                    handle_socket(
                        sock,
                        q,
                        fixed_alias,
                        alias_map=UDP_ALIAS_MAP if not fixed_alias else None,
                        ingress_policy=ingress_policy,
                    )
                )
            )

        # Mixer + Forwarder
        runtime_tasks.append(asyncio.create_task(mixer_loop(input_queues, mixer_queue)))
        await forward_loop(
            mixer_queue,
            routing_state=routing_state,
            processor=data_plane_processor,
        )
    finally:
        await _cancel_and_await_tasks(runtime_tasks)
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
