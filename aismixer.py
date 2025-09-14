import asyncio
import socket
import yaml
import os
import time
from assembler import AIVDMAssembler
from meta_writer import wrap_with_meta
from meta_cleaner import extract_nmea_sentences
from forwarder import Forwarder
from dedup import Deduplicator
from core.event import IngressEvent
from core.s_policy import choose_s_value, parse_tag_pairs_before_index, extract_g_tuple
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

deduplicator = Deduplicator()
forwarder = Forwarder(FORWARDERS)
assembler = AIVDMAssembler()


async def mixer_loop(input_queues, output_queue):
    async def reader(q):
        while True:
            item = await q.get()
            await output_queue.put(item)
    tasks = [asyncio.create_task(reader(q)) for q in input_queues]
    await asyncio.gather(*tasks)


async def forward_loop(queue):
    # Контекст: (assembler_key, group_id) -> 's' от ранна част на мултипарт
    multipart_s_ctx: dict[tuple[str, str], str] = {}
    while True:
        ev: IngressEvent = await queue.get()

        # Zero-copy friendly: взимаме индекси към ev.raw_line, не търсим с find()
        for sl in extract_nmea_sentences(ev.raw_line, want_idx=True):
            pos = sl.start
            clean_line = ev.raw_line[sl.start:sl.end]

            # 1) ТАГ блокът точно преди изречението (ако има)
            if getattr(sl, "tag_end", -1) >= 0:
                tag_pairs = parse_tag_pairs_before_index(ev.raw_line, pos)
            else:
                tag_pairs = {}

            g_info = extract_g_tuple(tag_pairs)  # (part, total, gid) или None

            # ако този фрагмент носи s:, запази го за групата
            if 's' in tag_pairs and g_info:
                _, _, gid = g_info
                multipart_s_ctx[(ev.assembler_key, gid)] = tag_pairs['s']

            # 2) Сглобяване на мултипарт
            multipart = assembler.feed(ev.assembler_key, clean_line)
            if multipart is None:
                continue  # чакаме още части

            for i, full_line in enumerate(multipart):
                if not deduplicator.is_unique(full_line):
                    continue
                is_first = i == 0
                # 3) Определи incoming_s
                incoming_s = tag_pairs.get('s')
                if g_info:
                    part, total, gid = g_info
                    incoming_s = incoming_s or multipart_s_ctx.get((ev.assembler_key, gid))
                # 4) Построй финалното s
                s_value = choose_s_value(STATION_ID, ev.alias_for_s or incoming_s, ev.raw_line, ev.remote_ip)
                touch_s(s_value)  # TTL поддръжка за s
                wrapped_line = wrap_with_meta(full_line, s_value, is_first=is_first)

                if DEBUG:
                    print(f"{ts()} OUTPUT => {wrapped_line}")

                await forwarder.send(wrapped_line + '\r\n')

            # 5) Ако това е последната част от групата — чистим кеша
            if g_info:
                part, total, gid = g_info
                if part == total:
                    multipart_s_ctx.pop((ev.assembler_key, gid), None)


async def handle_socket(sock, queue, fixed_alias=None, alias_map=None):
    loop = asyncio.get_running_loop()
    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)
        source_ip, source_port = addr[:2]
        raw_line = data.decode(errors="ignore").strip()

        if DEBUG:
            source_fmt = format_source(source_ip, source_port)
            print(f"{ts()} INPUT {source_fmt} => {raw_line}")

        alias_for_s = fixed_alias or (
            alias_map.get(source_ip) if alias_map else None)
        assembler_key = f"{source_ip}:{source_port}"

        await queue.put(IngressEvent(kind="udp",
                                     alias_for_s=alias_for_s,
                                     remote_ip=source_ip,
                                     assembler_key=assembler_key,
                                     raw_line=raw_line))


async def main():
    input_queues = []
    mixer_queue = asyncio.Queue()

    # Secure входове
    for entry in SEC_INPUTS:
        q = asyncio.Queue()
        input_queues.append(q)
        ip = entry["listen_ip"]
        port = entry["listen_port"]
        sec_id = entry.get("id")
        print(f"{ts()} Secure listening on {format_source(ip, port)}")
        asyncio.create_task(secure_server(q, ip, port, sec_input_id=sec_id))

    # UDP входове
    for entry in UDP_INPUTS:
        q = asyncio.Queue()
        input_queues.append(q)
        ip = entry["listen_ip"]
        port = entry["listen_port"]
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, port))
        sock.setblocking(False)
        print(f"{ts()} Listening on {format_source(ip, port)}")
        # ако има id -> фиксиран alias за целия вход
        fixed_alias = entry.get("id")
        asyncio.create_task(handle_socket(
            sock, q, fixed_alias, alias_map=UDP_ALIAS_MAP if not fixed_alias else None))

    # Mixer + Forwarder
    asyncio.create_task(mixer_loop(input_queues, mixer_queue))
    await forward_loop(mixer_queue)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting.")
