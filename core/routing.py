"""Transport-agnostic source-zone routing primitives.

This module deliberately contains no network or runtime integration.  Source and
target identifiers are opaque strings; adapters are responsible for interpreting
their namespaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, TypeAlias


SourceId: TypeAlias = str
TargetId: TypeAlias = str
ZoneName: TypeAlias = str


class ZoneResolutionError(ValueError):
    """Base exception for invalid or unresolvable zone definitions."""


class UnknownZoneError(ZoneResolutionError):
    """Raised when a zone expression or route references an unknown zone."""


class CircularZoneReferenceError(ZoneResolutionError):
    """Raised when zone expressions form a reference cycle."""


@dataclass(frozen=True, slots=True)
class ZoneDefinition:
    """A named-zone expression.

    Exactly one expression must be supplied. ``include`` contains source IDs;
    the set-operation fields contain names of other zones.
    """

    include: tuple[SourceId, ...] | None = None
    union: tuple[ZoneName, ...] | None = None
    intersection: tuple[ZoneName, ...] | None = None
    difference: tuple[ZoneName, ...] | None = None

    def __post_init__(self) -> None:
        for field_name in ("include", "union", "intersection", "difference"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _as_string_tuple(value, field_name))


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    """A route from one named zone to one or more opaque target IDs."""

    name: str
    from_zone: ZoneName
    to: tuple[TargetId, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "to", _as_string_tuple(self.to, "to"))


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """Ordered route names and unique target IDs matched for one source."""

    route_names: tuple[str, ...]
    target_ids: tuple[TargetId, ...]


ZoneConfig: TypeAlias = ZoneDefinition | Mapping[str, Iterable[str]]
RouteConfig: TypeAlias = RouteDefinition | Mapping[str, object]
ResolvedZones: TypeAlias = dict[ZoneName, frozenset[SourceId]]


def resolve_zones(zones: Mapping[ZoneName, ZoneConfig]) -> ResolvedZones:
    """Resolve named zone expressions into immutable source-ID sets.

    Zone names are resolved in sorted order so cycle/unknown-reference errors are
    deterministic even when the input mapping has no meaningful iteration order.
    """

    definitions = {name: _coerce_zone_definition(name, value) for name, value in zones.items()}
    resolved: ResolvedZones = {}

    def resolve(zone_name: ZoneName, path: tuple[ZoneName, ...]) -> frozenset[SourceId]:
        if zone_name in resolved:
            return resolved[zone_name]
        if zone_name not in definitions:
            referenced_by = path[-1] if path else None
            if referenced_by is None:
                raise UnknownZoneError(f"Unknown zone {zone_name!r}.")
            raise UnknownZoneError(
                f"Zone {referenced_by!r} references unknown zone {zone_name!r}."
            )
        if zone_name in path:
            cycle_start = path.index(zone_name)
            cycle = path[cycle_start:] + (zone_name,)
            raise CircularZoneReferenceError(
                f"Circular zone reference detected: {' -> '.join(cycle)}."
            )

        definition = definitions[zone_name]
        expression_name, values = _zone_expression(zone_name, definition)
        next_path = path + (zone_name,)

        if expression_name == "include":
            result = frozenset(values)
        else:
            operands = [resolve(reference, next_path) for reference in values]
            result = _apply_operation(expression_name, operands)

        resolved[zone_name] = result
        return result

    for name in sorted(definitions):
        resolve(name, ())

    return {name: resolved[name] for name in sorted(resolved)}


def match_routes(
    source_id: SourceId,
    resolved_zones: Mapping[ZoneName, frozenset[SourceId] | set[SourceId]],
    routes: Sequence[RouteConfig],
) -> RoutingResult:
    """Return route names and target IDs matching ``source_id``.

    Routes and targets retain declaration order. A target referenced by multiple
    matching routes is returned once, at its first occurrence.
    """

    route_names: list[str] = []
    target_ids: list[TargetId] = []
    seen_targets: set[TargetId] = set()

    for raw_route in routes:
        route = _coerce_route_definition(raw_route)
        if route.from_zone not in resolved_zones:
            raise UnknownZoneError(
                f"Route {route.name!r} references unknown zone {route.from_zone!r}."
            )
        if source_id not in resolved_zones[route.from_zone]:
            continue

        route_names.append(route.name)
        for target_id in route.to:
            if target_id not in seen_targets:
                seen_targets.add(target_id)
                target_ids.append(target_id)

    return RoutingResult(tuple(route_names), tuple(target_ids))


def _as_string_tuple(value: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise TypeError(f"{field_name!r} must be a sequence of strings, not a string.")
    result = tuple(value)
    if not all(isinstance(item, str) for item in result):
        raise TypeError(f"{field_name!r} must contain only strings.")
    return result


def _coerce_zone_definition(zone_name: ZoneName, value: ZoneConfig) -> ZoneDefinition:
    if not isinstance(zone_name, str):
        raise TypeError("Zone names must be strings.")
    if isinstance(value, ZoneDefinition):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"Zone {zone_name!r} must be a mapping or ZoneDefinition.")

    valid_fields = {"include", "union", "intersection", "difference"}
    unknown_fields = set(value) - valid_fields
    if unknown_fields:
        unknown = ", ".join(sorted(str(field) for field in unknown_fields))
        raise ZoneResolutionError(f"Zone {zone_name!r} has unknown field(s): {unknown}.")

    return ZoneDefinition(**value)


def _zone_expression(
    zone_name: ZoneName, definition: ZoneDefinition
) -> tuple[str, tuple[str, ...]]:
    expressions = [
        (name, getattr(definition, name))
        for name in ("include", "union", "intersection", "difference")
        if getattr(definition, name) is not None
    ]
    if len(expressions) != 1:
        raise ZoneResolutionError(
            f"Zone {zone_name!r} must define exactly one of include, union, "
            "intersection, or difference."
        )
    return expressions[0]


def _apply_operation(
    operation: str, operands: Sequence[frozenset[SourceId]]
) -> frozenset[SourceId]:
    if not operands:
        return frozenset()
    if operation == "union":
        return frozenset().union(*operands)
    if operation == "intersection":
        return frozenset.intersection(*operands)
    if operation == "difference":
        result = operands[0]
        for operand in operands[1:]:
            result = result.difference(operand)
        return result
    raise AssertionError(f"Unsupported zone operation: {operation}")


def _coerce_route_definition(value: RouteConfig) -> RouteDefinition:
    if isinstance(value, RouteDefinition):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("Routes must be mappings or RouteDefinition instances.")

    try:
        name = value["name"]
        from_zone = value["from_zone"]
        targets = value["to"]
    except KeyError as exc:
        raise ValueError(f"Route is missing required field {exc.args[0]!r}.") from exc

    if not isinstance(name, str) or not isinstance(from_zone, str):
        raise TypeError("Route 'name' and 'from_zone' values must be strings.")
    if not isinstance(targets, Iterable):
        raise TypeError("Route 'to' must be a sequence of strings.")
    return RouteDefinition(name=name, from_zone=from_zone, to=targets)
