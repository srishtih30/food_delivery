"""Restaurant -> society serviceability (who may deliver where).

Three concepts are kept separate in this project:

* **Road connectivity**   - does a route exist? (``dijkstra`` / ``astar``)
* **Serviceability**      - is this restaurant *allowed* to deliver to this
                            society under the configured policy? (this module)
* **Driver assignment**   - which driver carries the order? (``dispatch.py``)

A route existing is necessary but not sufficient: a Miraj restaurant is not
serviceable for a Sangli society when the policy excludes Miraj -> Sangli,
even though the graph connects them.

Zones
-----
Every address in the dataset ends with its postal town and PIN, e.g.
``"..., Miraj 416415, Maharashtra"``. A location's zone is that town (one of
``config.zones``), unless ``zone_overrides`` sets it explicitly. Area labels
and coordinates are *not* used to grant serviceability on their own.

Checks, in order (the first failure is the rejection reason):

1. restaurant and society exist with the right kind  -> INVALID_RESTAURANT_OR_SOCIETY
2. restaurant is accepting orders                     -> RESTAURANT_NOT_ACCEPTING_ORDERS
3. pair is authorised (deny list > allow list > zone rules)
                                                      -> OUTSIDE_SERVICE_ZONE
4. a road route exists                                -> NO_ROAD_ROUTE
5. route distance <= max_distance_km                  -> DELIVERY_DISTANCE_EXCEEDED
6. route time <= max_time_min                         -> DELIVERY_TIME_EXCEEDED

Explicitly allowed pairs skip only the zone rule; distance and time limits
still apply to them.

The default policy (``config/serviceability.json``) is a synthetic
assumption for this project, not a real platform's service boundaries.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, fields
from enum import Enum

from src.dijkstra import dijkstra
from src.graph import METRIC_INDEX, Graph, Location

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "serviceability.json")


class Reason(str, Enum):
    """Why a (restaurant, society) pair is not serviceable."""

    INVALID_RESTAURANT_OR_SOCIETY = "INVALID_RESTAURANT_OR_SOCIETY"
    RESTAURANT_NOT_ACCEPTING_ORDERS = "RESTAURANT_NOT_ACCEPTING_ORDERS"
    OUTSIDE_SERVICE_ZONE = "OUTSIDE_SERVICE_ZONE"
    NO_ROAD_ROUTE = "NO_ROAD_ROUTE"
    DELIVERY_DISTANCE_EXCEEDED = "DELIVERY_DISTANCE_EXCEEDED"
    DELIVERY_TIME_EXCEEDED = "DELIVERY_TIME_EXCEEDED"


@dataclass
class ServiceabilityConfig:
    """Serviceability policy. ``None`` for a limit disables that limit."""

    zones: list[str] = field(
        default_factory=lambda: ["Sangli", "Miraj", "Kupwad"])
    zone_overrides: dict[str, str] = field(default_factory=dict)
    allow_same_zone: bool = True
    # (restaurant_zone, society_zone) pairs allowed across zones.
    # Keep in sync with config/serviceability.json (a test checks this).
    allowed_zone_pairs: set[tuple[str, str]] = field(
        default_factory=lambda: {("Kupwad", "Sangli"), ("Sangli", "Kupwad")})
    # (restaurant_id, society_id) pairs: explicit allow / deny.
    allowed_pairs: set[tuple[str, str]] = field(default_factory=set)
    denied_pairs: set[tuple[str, str]] = field(default_factory=set)
    disabled_restaurants: set[str] = field(default_factory=set)
    max_distance_km: float | None = 8.0
    max_time_min: float | None = 30.0
    route_metric: str = "time"

    def validate(self) -> None:
        """Raise ValueError for settings that could never take effect."""
        used = ({zone for pair in self.allowed_zone_pairs for zone in pair}
                | set(self.zone_overrides.values()))
        unknown = used - set(self.zones)
        if unknown:
            raise ValueError(f"unknown zone(s) {sorted(unknown)}; "
                             f"configured zones are {self.zones}")
        if self.route_metric not in METRIC_INDEX:
            raise ValueError(f"route_metric must be one of "
                             f"{sorted(METRIC_INDEX)}")
        for name in ("max_distance_km", "max_time_min"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))
                                      or not math.isfinite(value)
                                      or value < 0):
                raise ValueError(
                    f"{name} must be a non-negative number or null")

    @classmethod
    def from_dict(cls, data: dict) -> ServiceabilityConfig:
        # Keys starting with "_" are comments; anything else unknown is
        # most likely a typo that would silently fall back to a default.
        unknown_keys = sorted(k for k in data if not k.startswith("_")
                              and k not in {f.name for f in fields(cls)})
        if unknown_keys:
            raise ValueError(f"unknown serviceability setting(s): "
                             f"{unknown_keys}")

        def pairs(key: str) -> set[tuple[str, str]]:
            return {(str(a).strip(), str(b).strip())
                    for a, b in data.get(key, [])}

        def ids(values: list[str]) -> set[str]:
            return {v.strip().upper() for v in values}

        defaults = cls()
        config = cls(
            zones=list(data.get("zones", defaults.zones)),
            zone_overrides={k.strip().upper(): v for k, v in
                            data.get("zone_overrides", {}).items()},
            allow_same_zone=bool(data.get("allow_same_zone", True)),
            allowed_zone_pairs=(pairs("allowed_zone_pairs")
                                if "allowed_zone_pairs" in data
                                else defaults.allowed_zone_pairs),
            allowed_pairs={(a.upper(), b.upper())
                           for a, b in pairs("allowed_pairs")},
            denied_pairs={(a.upper(), b.upper())
                          for a, b in pairs("denied_pairs")},
            disabled_restaurants=ids(data.get("disabled_restaurants", [])),
            max_distance_km=data.get("max_distance_km",
                                     defaults.max_distance_km),
            max_time_min=data.get("max_time_min", defaults.max_time_min),
            route_metric=data.get("route_metric", defaults.route_metric),
        )
        config.validate()
        return config

    @classmethod
    def from_json(cls, path: str = DEFAULT_CONFIG_PATH
                  ) -> ServiceabilityConfig:
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


@dataclass(frozen=True)
class ServiceabilityResult:
    """Outcome of one serviceability check."""

    restaurant_id: str
    society_id: str
    serviceable: bool
    reason: Reason | None = None
    message: str = ""
    restaurant_zone: str | None = None
    society_zone: str | None = None
    distance_km: float | None = None
    time_min: float | None = None
    route: tuple[str, ...] = ()


class ServiceabilityPolicy:
    """Evaluates :class:`ServiceabilityConfig` against a graph.

    Results are cached per pair; call :meth:`clear_cache` after the road
    network changes (e.g. a road closure).
    """

    def __init__(self, graph: Graph,
                 config: ServiceabilityConfig | None = None) -> None:
        self.graph = graph
        self.config = config or ServiceabilityConfig()
        self._zone_pattern = re.compile(
            r"\b(" + "|".join(map(re.escape, self.config.zones))
            + r")\s+\d{6}\b", re.IGNORECASE)
        self._cache: dict[tuple[str, str], ServiceabilityResult] = {}

    @classmethod
    def from_json(cls, graph: Graph, path: str = DEFAULT_CONFIG_PATH
                  ) -> ServiceabilityPolicy:
        return cls(graph, ServiceabilityConfig.from_json(path))

    @classmethod
    def default(cls, graph: Graph) -> ServiceabilityPolicy:
        """Policy from ``config/serviceability.json``, or the built-in
        defaults if that file is absent."""
        if os.path.isfile(DEFAULT_CONFIG_PATH):
            return cls.from_json(graph)
        return cls(graph)

    def clear_cache(self) -> None:
        self._cache.clear()

    def zone_of(self, node_id: str) -> str | None:
        """Configured zone of a location (override, else postal town)."""
        if node_id in self.config.zone_overrides:
            return self.config.zone_overrides[node_id]
        location = self.graph.locations.get(node_id)
        if location is None:
            return None
        match = self._zone_pattern.search(location.address)
        if match is None:
            return None
        # Return the configured spelling of the zone.
        found = match.group(1).lower()
        return next(z for z in self.config.zones if z.lower() == found)

    def check(self, restaurant_id: str, society_id: str
              ) -> ServiceabilityResult:
        key = (restaurant_id, society_id)
        if key not in self._cache:
            self._cache[key] = self._evaluate(restaurant_id, society_id)
        return self._cache[key]

    def is_serviceable(self, restaurant_id: str, society_id: str) -> bool:
        return self.check(restaurant_id, society_id).serviceable

    # ------------------------------------------------------------------

    def _location(self, node_id: str, kind: str) -> Location | None:
        location = self.graph.locations.get(node_id)
        return location if location is not None and location.kind == kind \
            else None

    def _authorised(self, restaurant_id: str, society_id: str,
                    r_zone: str | None, s_zone: str | None
                    ) -> tuple[bool, str]:
        pair = (restaurant_id, society_id)
        cfg = self.config
        if pair in cfg.denied_pairs:
            return False, "pair is on the deny list"
        if pair in cfg.allowed_pairs:
            return True, ""
        if r_zone is None or s_zone is None:
            return False, "location has no configured service zone"
        if r_zone == s_zone and cfg.allow_same_zone:
            return True, ""
        if (r_zone, s_zone) in cfg.allowed_zone_pairs:
            return True, ""
        return False, f"{r_zone} -> {s_zone} deliveries are not configured"

    def _evaluate(self, restaurant_id: str, society_id: str
                  ) -> ServiceabilityResult:
        cfg = self.config

        def reject(reason: Reason, message: str,
                   **extra) -> ServiceabilityResult:
            return ServiceabilityResult(restaurant_id, society_id, False,
                                        reason, message, **extra)

        restaurant = self._location(restaurant_id, "Restaurant")
        society = self._location(society_id, "Society")
        if restaurant is None or society is None:
            bad = [f"{node_id} is not a known {kind.lower()}"
                   for node_id, kind, loc in
                   ((restaurant_id, "Restaurant", restaurant),
                    (society_id, "Society", society)) if loc is None]
            return reject(Reason.INVALID_RESTAURANT_OR_SOCIETY,
                          "; ".join(bad))

        if restaurant_id in cfg.disabled_restaurants:
            return reject(Reason.RESTAURANT_NOT_ACCEPTING_ORDERS,
                          f"{restaurant_id} is not accepting orders")

        zones = {"restaurant_zone": self.zone_of(restaurant_id),
                 "society_zone": self.zone_of(society_id)}
        authorised, why = self._authorised(restaurant_id, society_id,
                                           zones["restaurant_zone"],
                                           zones["society_zone"])
        if not authorised:
            return reject(Reason.OUTSIDE_SERVICE_ZONE, why, **zones)

        route = dijkstra(self.graph, restaurant_id, society_id,
                         cfg.route_metric)
        if not route.found:
            return reject(Reason.NO_ROAD_ROUTE,
                          f"no road route {restaurant_id} -> {society_id}",
                          **zones)
        measured = {**zones, "distance_km": route.total_distance_km,
                    "time_min": route.total_time_min,
                    "route": tuple(route.path)}

        if (cfg.max_distance_km is not None
                and route.total_distance_km > cfg.max_distance_km):
            return reject(Reason.DELIVERY_DISTANCE_EXCEEDED,
                          f"{route.total_distance_km:.2f} km > "
                          f"{cfg.max_distance_km} km limit", **measured)
        if (cfg.max_time_min is not None
                and route.total_time_min > cfg.max_time_min):
            return reject(Reason.DELIVERY_TIME_EXCEEDED,
                          f"{route.total_time_min:.1f} min > "
                          f"{cfg.max_time_min} min limit", **measured)

        return ServiceabilityResult(restaurant_id, society_id, True,
                                    message="serviceable", **measured)
