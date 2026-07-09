from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class NetworkPolicyConfigError(ValueError):
    """Raised when an ingress network policy entry is invalid."""


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Immutable source-address allow-list for UDP-style ingress."""

    _networks: tuple[IPNetwork, ...] | None

    @classmethod
    def unrestricted(cls) -> "NetworkPolicy":
        return cls(None)

    @classmethod
    def deny_all(cls) -> "NetworkPolicy":
        return cls(())

    @classmethod
    def from_entries(
        cls,
        entries: Sequence[object],
        *,
        context: str = "allow_from",
    ) -> "NetworkPolicy":
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            raise NetworkPolicyConfigError(
                f"{context}: allow_from must be a list of IP addresses or CIDR networks"
            )
        if not entries:
            return cls.deny_all()

        networks = tuple(_compile_network_entry(entry, context) for entry in entries)
        return cls(networks)

    @property
    def is_unrestricted(self) -> bool:
        return self._networks is None

    @property
    def is_deny_all(self) -> bool:
        return self._networks == ()

    @property
    def networks(self) -> tuple[IPNetwork, ...]:
        return self._networks or ()

    def allows(self, peer_ip: object) -> bool:
        if self._networks is None:
            return True

        try:
            address = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False

        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped

        return any(
            address.version == network.version and address in network
            for network in self._networks
        )


def compile_ingress_policy(
    entry: Mapping[str, object],
    *,
    context: str,
) -> NetworkPolicy:
    if "allow_from" not in entry:
        return NetworkPolicy.unrestricted()

    allow_from = entry["allow_from"]
    if allow_from is None:
        raise NetworkPolicyConfigError(
            f"{context}.allow_from: allow_from must be a list of IP addresses or CIDR networks"
        )
    return NetworkPolicy.from_entries(
        allow_from,
        context=f"{context}.allow_from",
    )


def _compile_network_entry(
    entry: object,
    context: str,
) -> IPNetwork:
    if not isinstance(entry, str) or not entry.strip():
        raise NetworkPolicyConfigError(
            f"{context}: invalid allow_from entry {entry!r}"
        )

    value = entry.strip()
    try:
        if "/" in value:
            return ipaddress.ip_network(value)
        address = ipaddress.ip_address(value)
        return ipaddress.ip_network(address)
    except ValueError as exc:
        raise NetworkPolicyConfigError(
            f"{context}: invalid allow_from entry {value!r}"
        ) from exc
