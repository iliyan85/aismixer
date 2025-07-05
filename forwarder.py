import asyncio


class Forwarder:
    def __init__(self, targets):
        # targets: списък от (host, port)
        self.targets = targets
        self.transports = {}

    async def _ensure_transport(self, loop, host, port):
        key = (host, port)
        if key not in self.transports:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: asyncio.DatagramProtocol(),
                remote_addr=(host, port)
            )
            self.transports[key] = transport
        return self.transports[key]

    async def send(self, message):
        loop = asyncio.get_running_loop()
        #for host, port in self.targets:
        for entry in self.targets:
            host = entry['host']
            port = entry['port']
            transport = await self._ensure_transport(loop, host, port)
            transport.sendto(message.encode())
