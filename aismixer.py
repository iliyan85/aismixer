import asyncio
import socket
import yaml
import os
import time
import re
from assembler import AIVDMAssembler
from meta_writer import wrap_with_meta
from meta_cleaner import extract_nmea_sentences
from forwarder import Forwarder
from dedup import Deduplicator
from aismixer_secure import secure_server

try:
    from setproctitle import setproctitle
    setproctitle('aismixer')
except ImportError:
    pass  # No effect on Windows or if not installed

_ALNUM_SAFE = re.compile(r'[^A-Za-z0-9_]')


def _sanitize_s(val: str) -> str:
    """Допустими за TAG s: [A-Za-z0-9_]; отрежи до 15."""
    val = (val or "").strip()
    val = _ALNUM_SAFE.sub('_', val)
    return val[:15]


def _extract_incoming_s(raw: str) -> str | None:
    if not raw or raw[0] != '\\':
        return None
    try:
        # вземи между първата '\' и следващата '\' (TAG тялото с *CS)
        end = raw.find('\\', 1)
        if end == -1:
            return None
        body = raw[1:end]           # "k1:v1,k2:v2*CS"
        body = body.split('*', 1)[0]  # "k1:v1,k2:v2"
        for pair in body.split(','):
            if not pair:
                continue
            if ':' not in pair:
                continue
            k, v = pair.split(':', 1)
            if k == 's':
                return v
    except Exception:
        return None
    return None


def choose_s_value(global_station_id: str | None,
                   source_name_or_id: str | None,
                   incoming_s: str | None,
                   remote_ip: str | None) -> str:
    """
    Прилага приоритета:
      1) station_id (ако е непразно)
      2) input.id / alias / SEC client name (ако е непразно и не е 'ANONYMOUS')
      3) incoming_s от входния TAG (ако има)
      4) sanitized(remote_ip) като последен fallback
    Всичко се sanitize-ва и се реже до 15.
    """
    # 1) глобален station_id
    if global_station_id:
        return _sanitize_s(global_station_id)
    # 2) id/alias/name (ако имаме смислена стойност)
    if source_name_or_id and source_name_or_id != "ANONYMOUS":
        return _sanitize_s(source_name_or_id)
    # 3) s от входа (ако има)
    if incoming_s:
        return _sanitize_s(incoming_s)
    # 4) по IP
    if remote_ip:
        # IPv4: 1.2.3.4 -> 1_2_3_4 ; IPv6: 2001:db8::1 -> 2001_db8__1
        ip_sanitized = remote_ip.replace('.', '_').replace(':', '_')
        return _sanitize_s(ip_sanitized)
    # ако наистина няма нищо
    return _sanitize_s("ANONYMOUS")


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
    while True:
        source_name, remote_ip, raw_line = await queue.get()

        for clean_line in extract_nmea_sentences(raw_line):
            if not clean_line:
                continue

            multipart = assembler.feed(source_name, clean_line)
            if multipart is None:
                continue  # waiting for more parts or incomplete

            for i, full_line in enumerate(multipart):
                if not deduplicator.is_unique(full_line):
                    continue
                is_first = i == 0
                incoming_s = _extract_incoming_s(raw_line)
                s_value = choose_s_value(
                    # глобално station_id (може да е '')
                    STATION_ID,
                    source_name,            # id/alias/name/ANONYMOUS/IP
                    incoming_s,             # ако входът носи свое s:
                    remote_ip               # дефолтен fallback
                )
                wrapped_line = wrap_with_meta(
                    full_line, s_value, is_first=is_first)

                if DEBUG:
                    print(f"{ts()} OUTPUT => {wrapped_line}")

                await forwarder.send(wrapped_line + '\r\n')


async def handle_socket(sock, queue, fixed_alias=None, alias_map=None):
    loop = asyncio.get_running_loop()
    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)
        source_ip, source_port = addr[:2]
        raw_line = data.decode(errors="ignore").strip()

        if DEBUG:
            source_fmt = format_source(source_ip, source_port)
            print(f"{ts()} INPUT {source_fmt} => {raw_line}")

        alias = fixed_alias or (alias_map.get(
            source_ip) if alias_map else None)

        await queue.put((alias, source_ip, raw_line))


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
