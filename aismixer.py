import asyncio
import socket
import yaml
import os
import time
from assembler import AIVDMAssembler, AssemblyKey, AssemblyStatus
from meta_writer import wrap_with_meta
from secrets import randbelow
from forwarder import Forwarder
from dedup import Deduplicator
from core.event import IngressEvent
from core.ingress_frame import frame_from_ingress_event
from core.network_policy import NetworkPolicy, compile_ingress_policy
from core.parsed_sentence import (
    parse_frame_sentences,
    parse_leading_s_value,
)
from core.runtime_control import build_optional_routing_control_server
from core.runtime_routing import load_optional_routing_table
from core.routing_state import RoutingState
from core.s_policy import choose_s_value_from_candidates
from core.source_identity import build_udp_source_id
from core.state.s_cache import touch_s
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


def _gen_numeric_gid_fixed(digits: int) -> str:
    """
    Криптографски сигурно чисто числово gid с фиксирана дължина (без водещи нули).
    Равномерно в интервала [10^(d-1) .. 10^d - 1].
    """
    base = 10 ** (digits - 1)
    return str(base + randbelow(9 * base))


deduplicator = Deduplicator()
forwarder = Forwarder(FORWARDERS)
initial_routing_table = load_optional_routing_table(config, forwarder.target_ids)
routing_state = RoutingState(initial_routing_table)
assembler = AIVDMAssembler()


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


async def forward_loop(queue, routing_state=None):
    """Forward events using one immutable routing snapshot per IngressEvent.

    RoutingState replacements affect the next event pulled from the queue, not
    the event currently being processed.
    """

    # Multipart metadata follows the assembler's correlation identity.
    multipart_s_ctx: dict[AssemblyKey, str] = {}
    multipart_c_ctx: dict[AssemblyKey, int] = {}
    multipart_gid_ctx: dict[AssemblyKey, frozenset[str]] = {}

    def _discard_multipart_contexts(
        keys: tuple[AssemblyKey, ...],
    ) -> None:
        for key in keys:
            multipart_s_ctx.pop(key, None)
            multipart_c_ctx.pop(key, None)
            multipart_gid_ctx.pop(key, None)

    while True:
        ev: IngressEvent = await queue.get()
        frame = frame_from_ingress_event(ev)
        if frame is None:
            continue

        event_routing_table = None
        if routing_state is not None:
            event_routing_table = routing_state.snapshot().table
        route_target_ids = ()
        if event_routing_table is not None:
            route_target_ids = event_routing_table.match(
                frame.source_id
            ).target_ids

        event_leading_s = parse_leading_s_value(frame)
        parsed_sentences = parse_frame_sentences(
            frame,
            include_vdo=True,
        )
        for parsed in parsed_sentences:
            g_value = parsed.tag.g_value
            current_ingress_gid = (
                g_value.preservable_group_id
                if g_value is not None
                else None
            )

            # Use the parsed TAG c for direct single-sentence output
            # and deterministic multipart timestamp selection below.
            valid_c = (
                parsed.tag.c_value
                if C_PRESERVE_INGRESS_C
                else None
            )
            ts_for_header = valid_c

            # 2) Сглобяване на мултипарт
            outcome = assembler.feed_parsed_outcome(parsed)

            # Discarded generations must be cleared before the current
            # observation can establish metadata for a fresh generation.
            _discard_multipart_contexts(outcome.discarded_keys)

            if (
                outcome.status in {
                    AssemblyStatus.PENDING,
                    AssemblyStatus.DUPLICATE,
                    AssemblyStatus.COMPLETE,
                }
                and outcome.group_key is not None
                and valid_c is not None
            ):
                previous_c = multipart_c_ctx.get(outcome.group_key)
                multipart_c_ctx[outcome.group_key] = (
                    valid_c
                    if previous_c is None
                    else min(previous_c, valid_c)
                )

            if (
                outcome.status in {
                    AssemblyStatus.PENDING,
                    AssemblyStatus.DUPLICATE,
                    AssemblyStatus.COMPLETE,
                }
                and outcome.group_key is not None
                and G_PRESERVE_INGRESS_GID
                and current_ingress_gid is not None
            ):
                previous_gids = multipart_gid_ctx.get(
                    outcome.group_key,
                    frozenset(),
                )
                multipart_gid_ctx[outcome.group_key] = (
                    previous_gids | frozenset((current_ingress_gid,))
                )

            if (
                outcome.status
                in {AssemblyStatus.PENDING, AssemblyStatus.DUPLICATE}
                and outcome.group_key is not None
                and parsed.tag.s_value is not None
                and g_value is not None
            ):
                multipart_s_ctx[outcome.group_key] = parsed.tag.s_value

            if outcome.status in {
                AssemblyStatus.INVALID,
                AssemblyStatus.LIMIT_EXCEEDED,
                AssemblyStatus.PENDING,
                AssemblyStatus.DUPLICATE,
                AssemblyStatus.CONFLICT,
            }:
                continue

            multipart = outcome.sentences

            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                selected_c = (
                    multipart_c_ctx.get(outcome.group_key)
                    if C_PRESERVE_INGRESS_C
                    else None
                )
                # wrap_with_meta treats integer zero as a server-time fallback.
                # Keep single-sentence behavior unchanged while rendering a
                # valid multipart c:0 selected by the group-level policy.
                ts_for_header = "0" if selected_c == 0 else selected_c

            # --- изходно gid за тази група ---
            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                observed_gids = multipart_gid_ctx.get(
                    outcome.group_key,
                    frozenset(),
                )
                if G_PRESERVE_INGRESS_GID and len(observed_gids) == 1:
                    out_gid = next(iter(observed_gids))
                else:
                    out_gid = _gen_numeric_gid_fixed(G_ID_DIGITS)
            elif G_PRESERVE_INGRESS_GID and current_ingress_gid is not None:
                out_gid = current_ingress_gid
            else:
                out_gid = _gen_numeric_gid_fixed(G_ID_DIGITS)

            total_parts = len(multipart)
            tag_single = (total_parts == 1 and G_ALWAYS_TAG_SINGLE)

            logical_key = multipart[0] if total_parts == 1 else tuple(multipart)
            eligible_target_ids = None
            if event_routing_table is None:
                emit_group = deduplicator.is_unique(logical_key)
            else:
                eligible_target_ids = tuple(
                    target_id
                    for target_id in route_target_ids
                    if deduplicator.is_unique(logical_key, scope=target_id)
                )
                emit_group = bool(eligible_target_ids)

            incoming_s = parsed.tag.s_value
            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                incoming_s = incoming_s or multipart_s_ctx.get(
                    outcome.group_key
                )

            for i, full_line in enumerate(multipart if emit_group else ()):
                is_first = i == 0
                # 3) Построй финалното s
                source_name_or_id = frame.alias_for_s or incoming_s
                s_value = choose_s_value_from_candidates(
                    STATION_ID,
                    source_name_or_id,
                    event_leading_s,
                    frame.remote_ip,
                )
                touch_s(s_value)  # TTL поддръжка за s

                # g: добавяме при multipart или ако е разрешено и за single
                if total_parts > 1 or tag_single:
                    g_triplet = f"{i+1}-{total_parts}-{out_gid}"
                else:
                    g_triplet = None
                wrapped_line = wrap_with_meta(
                    full_line, s_value, ts_for_header, is_first=is_first, g_triplet=g_triplet)

                if DEBUG:
                    print(f"{ts()} OUTPUT => {wrapped_line}")

                if event_routing_table is None:
                    await forwarder.send(wrapped_line + '\r\n')
                else:
                    await forwarder.send_to(
                        eligible_target_ids,
                        wrapped_line + '\r\n',
                    )

            # 4) Successful multipart completion consumes its metadata.
            if (
                outcome.status is AssemblyStatus.COMPLETE
                and outcome.group_key is not None
            ):
                _discard_multipart_contexts((outcome.group_key,))


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

        raw_line = data.decode(errors="ignore").strip()

        if DEBUG:
            source_fmt = format_source(source_ip, source_port)
            print(f"{ts()} INPUT {source_fmt} => {raw_line}")

        mapped_alias = alias_map.get(source_ip) if alias_map else None
        alias_for_s = fixed_alias or mapped_alias
        assembler_key = f"{source_ip}:{source_port}"
        source_id = build_udp_source_id(fixed_alias, mapped_alias, source_ip)

        await queue.put(IngressEvent(kind="udp",
                                     source_id=source_id,
                                     alias_for_s=alias_for_s,
                                     remote_ip=source_ip,
                                     assembler_key=assembler_key,
                                     raw_line=raw_line))


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
        await forward_loop(mixer_queue, routing_state=routing_state)
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
