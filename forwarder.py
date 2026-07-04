import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from core.target_identity import build_udp_target_id


@dataclass(frozen=True, slots=True)
class _UdpDestination:
    host: str
    port: int


class ForwarderConfigError(ValueError):
    """Raised when UDP forwarder configuration is invalid."""


class UnknownForwarderTargetError(ValueError):
    """Raised when targeted UDP sending references an unknown target."""


class Forwarder:
    def __init__(self, targets):
        self._targets = tuple(_copy_target_entry(entry) for entry in targets)
        self.targets = self._targets
        self._destinations = tuple(
            _UdpDestination(entry["host"], entry["port"]) for entry in self._targets
        )

        registry: dict[str, _UdpDestination] = {}
        for entry, destination in zip(self._targets, self._destinations):
            if "id" not in entry:
                continue
            target_id = build_udp_target_id(entry["id"])
            if target_id in registry:
                raise ForwarderConfigError(f"Duplicate UDP forwarder target ID: {target_id}")
            registry[target_id] = destination

        self._target_registry = MappingProxyType(registry)
        self._target_ids = tuple(registry)
        self.transports = {}

    @property
    def target_ids(self):
        return self._target_ids

    async def _ensure_transport(self, loop, host, port):
        key = (host, port)
        if key not in self.transports:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: asyncio.DatagramProtocol(),
                remote_addr=(host, port)
            )
            self.transports[key] = transport
        return self.transports[key]

    async def _send_to_destination(self, loop, destination, message):
        transport = await self._ensure_transport(loop, destination.host, destination.port)
        transport.sendto(message.encode())

    async def send(self, message):
        loop = asyncio.get_running_loop()
        for destination in self._destinations:
            await self._send_to_destination(loop, destination, message)

    async def send_to(self, target_ids, message):
        loop = asyncio.get_running_loop()
        for target_id in _dedupe_target_ids(target_ids):
            try:
                destination = self._target_registry[target_id]
            except KeyError as exc:
                raise UnknownForwarderTargetError(
                    f"Unknown UDP forwarder target ID: {target_id}"
                ) from exc
            await self._send_to_destination(loop, destination, message)


def _copy_target_entry(entry: Mapping[str, object]) -> Mapping[str, object]:
    copied = {
        "host": entry["host"],
        "port": entry["port"],
    }
    if "id" in entry:
        copied["id"] = entry["id"]
    return MappingProxyType(copied)


def _dedupe_target_ids(target_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(target_ids, str):
        raise TypeError("target_ids must be an iterable of target ID strings.")

    ordered: list[str] = []
    seen: set[str] = set()
    for target_id in target_ids:
        if target_id in seen:
            continue
        seen.add(target_id)
        ordered.append(target_id)
    return tuple(ordered)
