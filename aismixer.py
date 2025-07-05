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


config = load_config()

SEC_INPUTS = config.get("sec_inputs", [])
UDP_INPUTS = config.get("udp_inputs", [])
FORWARDERS = config.get("forwarders", [])
STATION_ID = config.get("station_id", "mixstation_1")
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
        source_ip, raw_line = await queue.get()

        for clean_line in extract_nmea_sentences(raw_line):
            if not clean_line:
                continue

            multipart = assembler.feed(source_ip, clean_line)
            if multipart is None:
                continue  # waiting for more parts or incomplete

            for i, full_line in enumerate(multipart):
                if not deduplicator.is_unique(full_line):
                    continue
                is_first = i == 0
                wrapped_line = wrap_with_meta(
                    full_line, STATION_ID, is_first=is_first)

                if DEBUG:
                    print(f"{ts()} OUTPUT => {wrapped_line}")

                await forwarder.send(wrapped_line + '\r\n')


async def handle_socket(sock, queue):
    loop = asyncio.get_running_loop()
    while True:
        data, addr = await loop.sock_recvfrom(sock, 8192)
        source_ip, source_port = addr[:2]
        raw_line = data.decode(errors="ignore").strip()

        if DEBUG:
            source_fmt = format_source(source_ip, source_port)
            print(f"{ts()} INPUT {source_fmt} => {raw_line}")

        await queue.put((source_ip, raw_line))


async def main():
    input_queues = []
    mixer_queue = asyncio.Queue()

    # Secure входове
    for entry in SEC_INPUTS:
        q = asyncio.Queue()
        input_queues.append(q)
        ip = entry["listen_ip"]
        port = entry["listen_port"]
        print(f"{ts()} Secure listening on {format_source(ip, port)}")
        asyncio.create_task(secure_server(q, ip, port))

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
        asyncio.create_task(handle_socket(sock, q))

    # Mixer + Forwarder
    asyncio.create_task(mixer_loop(input_queues, mixer_queue))
    await forward_loop(mixer_queue)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting.")
