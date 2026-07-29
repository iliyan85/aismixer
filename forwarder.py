import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from core.target_identity import EgressTargetId, build_udp_target_id


@dataclass(frozen=True, slots=True)
class _UdpDestination:
    host: str
    port: int
    source_ip: str | None = None
    family: int = socket.AF_UNSPEC


class ForwarderConfigError(ValueError):
    """Raised when UDP forwarder configuration is invalid."""


class UnknownForwarderTargetError(ValueError):
    """Raised when targeted UDP sending references an unknown target."""


class Forwarder:
    def __init__(self, targets):
        self._targets = tuple(_copy_target_entry(entry) for entry in targets)
        self.targets = self._targets
        self._destinations = tuple(
            _destination_from_entry(entry) for entry in self._targets
        )
        self._all_target_ids = tuple(range(len(self._destinations)))

        target_id_by_name: dict[str, EgressTargetId] = {}
        for target_id, entry in enumerate(self._targets):
            if "id" not in entry:
                continue
            target_name = build_udp_target_id(entry["id"])
            if target_name in target_id_by_name:
                raise ForwarderConfigError(
                    f"Duplicate UDP forwarder target ID: {target_name}"
                )
            target_id_by_name[target_name] = target_id

        self._target_id_by_name = MappingProxyType(target_id_by_name)
        self._target_ids = tuple(target_id_by_name)
        self.transports = {}

    @property
    def target_ids(self):
        return self._target_ids

    @property
    def all_target_ids(self) -> tuple[EgressTargetId, ...]:
        return self._all_target_ids

    @property
    def target_id_by_name(self) -> Mapping[str, EgressTargetId]:
        return self._target_id_by_name

    async def _ensure_transport(self, loop, destination):
        key = _transport_cache_key(destination)
        if key not in self.transports:
            kwargs = {"remote_addr": (destination.host, destination.port)}
            if destination.source_ip is not None:
                kwargs["family"] = destination.family
                kwargs["local_addr"] = (destination.source_ip, 0)
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: asyncio.DatagramProtocol(),
                    **kwargs,
                )
            except OSError as exc:
                raise ForwarderConfigError(
                    "Could not create UDP forwarder transport for "
                    f"{destination.host}:{destination.port}"
                    + (
                        f" from source_ip {destination.source_ip}"
                        if destination.source_ip
                        else ""
                    )
                ) from exc
            self.transports[key] = transport
        return self.transports[key]

    async def _send_to_destination(self, loop, destination, message):
        transport = await self._ensure_transport(loop, destination)
        transport.sendto(message.encode())

    async def _dispatch_to_ids(
        self,
        target_ids: Iterable[EgressTargetId],
        message,
    ) -> None:
        loop = asyncio.get_running_loop()
        for target_id in target_ids:
            await self._send_to_destination(
                loop,
                self._destinations[target_id],
                message,
            )

    async def send(self, message):
        await self._dispatch_to_ids(self._all_target_ids, message)

    def close(self):
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()

    async def send_to_ids(
        self,
        target_ids: Iterable[EgressTargetId],
        message,
    ) -> None:
        validated_target_ids = _validate_numeric_target_ids(
            target_ids,
            len(self._destinations),
        )
        await self._dispatch_to_ids(validated_target_ids, message)

    async def send_to(self, target_ids, message):
        loop = asyncio.get_running_loop()
        for target_id in _dedupe_target_ids(target_ids):
            try:
                numeric_target_id = self._target_id_by_name[target_id]
            except KeyError as exc:
                raise UnknownForwarderTargetError(
                    f"Unknown UDP forwarder target ID: {target_id}"
                ) from exc
            destination = self._destinations[numeric_target_id]
            await self._send_to_destination(loop, destination, message)


def _copy_target_entry(entry: Mapping[str, object]) -> Mapping[str, object]:
    copied = {
        "host": entry["host"],
        "port": entry["port"],
    }
    if "id" in entry:
        copied["id"] = entry["id"]
    if "source_ip" in entry:
        copied["source_ip"] = _normalize_source_ip(entry["source_ip"], entry)
    return MappingProxyType(copied)


def _destination_from_entry(entry: Mapping[str, object]) -> _UdpDestination:
    host = str(entry["host"])
    port = int(entry["port"])
    source_ip = entry.get("source_ip")
    if source_ip is None:
        return _UdpDestination(host, port)

    source_address = ipaddress.ip_address(source_ip)
    family = socket.AF_INET6 if source_address.version == 6 else socket.AF_INET
    host_address = _parse_literal_host(host)
    if host_address is not None and host_address.version != source_address.version:
        context = _target_context(entry)
        raise ForwarderConfigError(
            f"{context}: source_ip {source_ip!r} is IPv{source_address.version} "
            f"but host {host!r} is IPv{host_address.version}"
        )

    return _UdpDestination(host, port, str(source_address), family)


def _normalize_source_ip(value: object, entry: Mapping[str, object]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForwarderConfigError(
            f"{_target_context(entry)}: source_ip must be a literal IPv4 or IPv6 address"
        )
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise ForwarderConfigError(
            f"{_target_context(entry)}: invalid source_ip {value!r}"
        ) from exc


def _parse_literal_host(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _target_context(entry: Mapping[str, object]) -> str:
    if "id" in entry:
        return f"forwarder {entry['id']!r}"
    return f"forwarder {entry.get('host')!r}:{entry.get('port')!r}"


def _transport_cache_key(destination: _UdpDestination):
    if destination.source_ip is None:
        return (destination.host, destination.port)
    return (destination.host, destination.port, destination.source_ip)


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


def _validate_numeric_target_ids(
    target_ids: Iterable[EgressTargetId],
    destination_count: int,
) -> tuple[EgressTargetId, ...]:
    if isinstance(target_ids, (str, bytes)):
        raise TypeError(
            "target_ids must be an iterable of integer egress target IDs."
        )

    ordered: list[EgressTargetId] = []
    seen: set[EgressTargetId] = set()
    for target_id in target_ids:
        if isinstance(target_id, bool) or not isinstance(target_id, int):
            raise TypeError(
                "UDP forwarder numeric target IDs must be integers, "
                f"got {type(target_id).__name__}."
            )
        if target_id < 0 or target_id >= destination_count:
            raise UnknownForwarderTargetError(
                f"Unknown UDP forwarder target ID: {target_id}"
            )
        if target_id in seen:
            continue
        seen.add(target_id)
        ordered.append(target_id)
    return tuple(ordered)
