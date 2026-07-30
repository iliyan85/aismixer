"""Transport-agnostic source-zone routing primitives.

This module deliberately contains no network or runtime integration.  Source and
target identifiers are opaque strings; adapters are responsible for interpreting
their namespaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence, TypeAlias

from core.target_identity import EgressTargetId, freeze_target_id_by_name


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
        if not isinstance(self.name, str) or not isinstance(self.from_zone, str):
            raise TypeError("Route 'name' and 'from_zone' values must be strings.")
        if not isinstance(self.to, Sequence) or isinstance(self.to, str):
            raise TypeError("Route 'to' must be a sequence of strings.")
        object.__setattr__(self, "to", _as_string_tuple(self.to, "to"))


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """Ordered route names and unique target IDs matched for one source."""

    route_names: tuple[str, ...]
    target_ids: tuple[TargetId, ...]


ZoneConfig: TypeAlias = ZoneDefinition | Mapping[str, Iterable[str]]
RouteConfig: TypeAlias = RouteDefinition | Mapping[str, object]
ResolvedZones: TypeAlias = dict[ZoneName, frozenset[SourceId]]


@dataclass(frozen=True, slots=True)
class _CompiledTargetRoute:
    """Immutable target-only route used by the runtime matching path."""

    source_ids: frozenset[SourceId]
    target_ids: tuple[EgressTargetId, ...]


@dataclass(frozen=True, slots=True)
class RoutingTable:
    """A compiled, reusable snapshot of zones and ordered routes."""

    resolved_zones: Mapping[ZoneName, frozenset[SourceId]]
    route_definitions: tuple[RouteDefinition, ...]
    _compiled_target_routes: tuple[_CompiledTargetRoute, ...] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        resolved_zones = {
            name: frozenset(source_ids)
            for name, source_ids in self.resolved_zones.items()
        }
        route_definitions = tuple(self.route_definitions)
        _validate_route_zone_references(resolved_zones, route_definitions)
        object.__setattr__(self, "resolved_zones", MappingProxyType(resolved_zones))
        object.__setattr__(self, "route_definitions", route_definitions)
        object.__setattr__(self, "_compiled_target_routes", None)

    @property
    def has_compiled_target_plan(self) -> bool:
        """Return whether numeric target compilation has completed."""

        return self._compiled_target_routes is not None

    @classmethod
    def from_definitions(
        cls,
        zones: Mapping[ZoneName, ZoneConfig],
        routes: Sequence[RouteConfig],
    ) -> RoutingTable:
        """Compile already structured zone and route definitions."""

        return cls(
            resolved_zones=resolve_zones(zones),
            route_definitions=tuple(load_route_definitions(routes)),
        )

    @classmethod
    def from_config(
        cls,
        zones_config: Mapping[str, object],
        routes_config: Sequence[RouteConfig],
    ) -> RoutingTable:
        """Load and compile plain YAML/JSON-shaped routing config."""

        zone_definitions = load_zone_definitions(zones_config)
        route_definitions = load_route_definitions(routes_config)
        return cls(
            resolved_zones=resolve_zones(zone_definitions),
            route_definitions=tuple(route_definitions),
        )

    def match(self, source_id: SourceId) -> RoutingResult:
        """Match a source against the compiled zones and routes."""

        return match_routes(source_id, self.resolved_zones, self.route_definitions)

    def compile_target_ids(
        self,
        target_id_by_name: Mapping[str, EgressTargetId],
    ) -> RoutingTable:
        """Return a new table with route target names resolved to numeric IDs."""

        frozen_target_ids = freeze_target_id_by_name(target_id_by_name)
        unknown = sorted({
            target_name
            for route in self.route_definitions
            for target_name in route.to
            if target_name not in frozen_target_ids
        })
        if unknown:
            raise ValueError(
                "Routing target name(s) are unavailable or unsupported: "
                f"{', '.join(unknown)}."
            )

        compiled_target_routes = tuple(
            _CompiledTargetRoute(
                source_ids=self.resolved_zones[route.from_zone],
                target_ids=tuple(
                    frozen_target_ids[target_name]
                    for target_name in route.to
                ),
            )
            for route in self.route_definitions
        )
        compiled_table = RoutingTable(
            resolved_zones=self.resolved_zones,
            route_definitions=self.route_definitions,
        )
        object.__setattr__(
            compiled_table,
            "_compiled_target_routes",
            compiled_target_routes,
        )
        return compiled_table

    def match_target_ids(
        self,
        source_id: SourceId,
    ) -> tuple[EgressTargetId, ...]:
        """Return ordered unique numeric targets without descriptive results."""

        compiled_target_routes = self._compiled_target_routes
        if compiled_target_routes is None:
            raise RuntimeError(
                "RoutingTable has no compiled numeric target plan; "
                "call compile_target_ids() first."
            )

        target_ids: list[EgressTargetId] = []
        seen_target_ids: set[EgressTargetId] = set()
        for route in compiled_target_routes:
            if source_id not in route.source_ids:
                continue
            for target_id in route.target_ids:
                if target_id in seen_target_ids:
                    continue
                seen_target_ids.add(target_id)
                target_ids.append(target_id)
        return tuple(target_ids)


def load_zone_definitions(config: Mapping[str, object]) -> dict[str, ZoneDefinition]:
    """Convert plain zone config mappings into validated definitions."""

    if not isinstance(config, Mapping):
        raise TypeError("Zones config must be a mapping.")

    definitions: dict[str, ZoneDefinition] = {}
    for name, value in config.items():
        definition = _coerce_zone_definition(name, value)
        _zone_expression(name, definition)
        definitions[name] = definition
    return definitions


def load_route_definitions(config: Sequence[RouteConfig]) -> list[RouteDefinition]:
    """Convert plain route config mappings into validated definitions."""

    if not isinstance(config, Sequence) or isinstance(config, str):
        raise TypeError("Routes config must be a sequence.")
    return [_coerce_route_definition(route) for route in config]


def validate_routing_config(
    zones_config: Mapping[str, object], routes_config: Sequence[RouteConfig]
) -> ResolvedZones:
    """Validate zone and route config and return fully resolved zones."""

    zone_definitions = load_zone_definitions(zones_config)
    route_definitions = load_route_definitions(routes_config)
    resolved_zones = resolve_zones(zone_definitions)

    _validate_route_zone_references(resolved_zones, route_definitions)

    return resolved_zones


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


def _validate_route_zone_references(
    resolved_zones: Mapping[ZoneName, frozenset[SourceId]],
    routes: Sequence[RouteDefinition],
) -> None:
    for route in routes:
        if route.from_zone not in resolved_zones:
            raise UnknownZoneError(
                f"Route {route.name!r} references unknown zone {route.from_zone!r}."
            )


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

    valid_fields = {"name", "from_zone", "to"}
    unknown_fields = set(value) - valid_fields
    if unknown_fields:
        unknown = ", ".join(sorted(str(field) for field in unknown_fields))
        raise ValueError(f"Route has unknown field(s): {unknown}.")

    try:
        name = value["name"]
        from_zone = value["from_zone"]
        targets = value["to"]
    except KeyError as exc:
        raise ValueError(f"Route is missing required field {exc.args[0]!r}.") from exc

    if not isinstance(name, str) or not isinstance(from_zone, str):
        raise TypeError("Route 'name' and 'from_zone' values must be strings.")
    if not isinstance(targets, Sequence) or isinstance(targets, str):
        raise TypeError("Route 'to' must be a sequence of strings.")
    return RouteDefinition(name=name, from_zone=from_zone, to=targets)
