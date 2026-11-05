"""One read-only road network, with isolated per-request simulation graphs."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.validation import identifier, integer, number, scenario
from src.data_loader import load_dataset
from src.dijkstra import dijkstra
from src.graph import METRIC_INDEX, METRIC_UNITS, build_graph
from src.heuristics import calibrate_scale
from src.relay import compare_strategies
from src.scenario import ScenarioSpec, run_scenario
from src.serviceability import ServiceabilityPolicy
from src.tracing import astar_with_trace, dijkstra_with_trace

ROOT = Path(__file__).resolve().parents[1]
MAX_CUSTOM_ORDERS = 6
MAX_FLEET_DRIVERS = 12


def _mercator(lat: float, lon: float) -> tuple[float, float]:
    return (lon + 180) / 360, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2


class StudioService:
    """Domain-facing adapter shared by every HTTP endpoint."""

    def __init__(self, data_dir: Path = ROOT / "data", config_path: Path = ROOT / "config" / "serviceability.json") -> None:
        self.data_dir = Path(data_dir)
        dataset = load_dataset(str(self.data_dir))
        self.graph = build_graph(dataset)
        self.policy = ServiceabilityPolicy.from_json(self.graph, str(config_path))
        self.scales = {metric: calibrate_scale(self.graph, metric) for metric in METRIC_INDEX}
        self.warnings = dataset.warnings
        self._links: list[dict[str, Any]] = []

        def optional_number(value: Any) -> float | None:
            try:
                result = float(value)
            except (ValueError, TypeError):
                return None
            return result if math.isfinite(result) and result >= 0 else None

        for row in dataset.society_restaurants.itertuples(index=False):
            s, r = row.Society_ID, row.Restaurant_ID
            if s not in self.graph.locations or r not in self.graph.locations:
                continue
            if self.graph.locations[s].kind != "Society" or self.graph.locations[r].kind != "Restaurant":
                continue
            self._links.append({
                "society_id": s, "restaurant_id": r,
                "rank": optional_number(getattr(row, "Rank", None)),
                "forward_km": optional_number(row.Distance_km), "forward_min": optional_number(row.Travel_Time_min),
                "return_km": optional_number(row.Return_Distance_km), "return_min": optional_number(row.Return_Travel_Time_min),
            })
        self.catalog_payload = self._catalog()
        self.graph_payload = self._graph_payload()
        self._fleet_cache = lru_cache(maxsize=8)(self._run_fleet)

    def _catalog(self) -> dict[str, Any]:
        by_society: dict[str, set[str]] = {}
        by_restaurant: dict[str, set[str]] = {}
        for link in self._links:
            by_society.setdefault(link["society_id"], set()).add(link["restaurant_id"])
            by_restaurant.setdefault(link["restaurant_id"], set()).add(link["society_id"])
        restaurants, societies = [], []
        for location in self.graph.locations.values():
            common = {"id": location.node_id, "name": location.name,
                      "area": location.area, "zone": self.policy.zone_of(location.node_id)}
            if location.kind == "Restaurant":
                restaurants.append({**common, "cuisine": location.cuisine,
                                    "rating": location.rating,
                                    "linked_societies": len(by_restaurant.get(location.node_id, set()))})
            else:
                societies.append({**common,
                                  "linked_restaurants": len(by_society.get(location.node_id, set()))})
        return {"restaurants": restaurants, "societies": societies, "links": self._links,
                "summary": {"restaurants": len(restaurants), "societies": len(societies),
                            "relationships": len(self._links), "roads": self.graph.edge_count},
                "policy": {"zones": self.policy.config.zones,
                           "allowed_zone_pairs": sorted(map(list, self.policy.config.allowed_zone_pairs)),
                           "max_distance_km": self.policy.config.max_distance_km,
                           "max_time_min": self.policy.config.max_time_min},
                "warnings": self.warnings}

    def _graph_payload(self) -> dict[str, Any]:
        locations = list(self.graph.locations.values())
        points = [_mercator(loc.latitude, loc.longitude) for loc in locations]
        min_x, min_y = min(p[0] for p in points), min(p[1] for p in points)
        span = max(max(p[0] - min_x for p in points), max(p[1] - min_y for p in points)) or 1
        unit = 1000 / span

        def to_svg(lat: float, lon: float) -> list[float]:
            x, y = _mercator(lat, lon)
            return [round((x - min_x) * unit, 2), round((y - min_y) * unit, 2)]

        nodes = [{"id": loc.node_id, "name": loc.name, "kind": loc.kind,
                  "area": loc.area, "cuisine": loc.cuisine,
                  "lat": loc.latitude, "lon": loc.longitude,
                  "x": round((x - min_x) * unit, 3), "y": round((y - min_y) * unit, 3)}
                 for loc, (x, y) in zip(locations, points)]
        pairs: dict[tuple[str, str], dict[str, Any]] = {}
        for source, neighbours in self.graph.adjacency.items():
            for target, weights in neighbours.items():
                a, b = sorted((source, target))
                pair = pairs.setdefault((a, b), {"a": a, "b": b, "ab": None, "ba": None})
                pair["ab" if source == a else "ba"] = list(weights)
        geometry_file = self.data_dir / "road_geometry.json"
        if geometry_file.is_file():
            with geometry_file.open(encoding="utf-8") as file:
                shapes = json.load(file).get("roads", {})
            for (a, b), pair in pairs.items():
                coords = shapes.get(f"{a}|{b}", {}).get("coords")
                if coords and len(coords) >= 2:
                    pair["shape"] = [to_svg(lat, lon) for lon, lat in coords]
        return {"nodes": nodes, "edges": list(pairs.values()),
                "width": round(max(n["x"] for n in nodes), 3),
                "height": round(max(n["y"] for n in nodes), 3),
                "projection": {"origin_x": min_x, "origin_y": min_y, "unit": unit},
                "stats": {"locations": len(nodes), "societies": len(self.catalog_payload["societies"]),
                          "restaurants": len(self.catalog_payload["restaurants"]), "roads": self.graph.edge_count},
                "street_shapes": sum("shape" in pair for pair in pairs.values()),
                "heuristic_scale": self.scales, "units": METRIC_UNITS, "warnings": self.warnings}

    def serviceability(self, restaurant: str, society: str) -> dict[str, Any]:
        restaurant, society = identifier(restaurant, "restaurant"), identifier(society, "society")
        result = self.policy.check(restaurant, society)
        payload = {**asdict(result), "restaurant": restaurant, "society": society,
                   "reason": result.reason.value if result.reason else None,
                   "route": list(result.route), "legs": self._legs(result.route),
                   "preview": None}
        # A zone/deny rejection never computes a route. For the map we still
        # show the road path the order *would* take, clearly marked as a
        # preview, so every distance on screen has a visible path.
        if not result.route and restaurant in self.graph.locations and society in self.graph.locations:
            found = dijkstra(self.graph, restaurant, society, self.policy.config.route_metric)
            if found.found:
                payload["preview"] = {"route": list(found.path), "legs": self._legs(found.path),
                                      "distance_km": round(found.total_distance_km, 3),
                                      "time_min": round(found.total_time_min, 2)}
        return payload

    def _legs(self, path: Any) -> list[dict[str, Any]]:
        """Per-hop distance/time along a node path (directional weights)."""
        legs = []
        for a, b in zip(path, list(path)[1:]):
            weights = self.graph.adjacency.get(a, {}).get(b)
            if weights is None:
                continue
            legs.append({"from": a, "to": b, "km": round(weights[0], 3), "min": round(weights[1], 2)})
        return legs

    def coverage(self, society: str) -> dict[str, Any]:
        society = identifier(society, "society")
        if society not in self.graph.locations or self.graph.locations[society].kind != "Society":
            raise ValueError(f"Unknown housing society: {society}.")
        linked = {l["restaurant_id"]: l for l in self._links if l["society_id"] == society}
        restaurants = []
        for restaurant in self.catalog_payload["restaurants"]:
            r = restaurant["id"]
            result = self.policy.check(r, society)
            restaurants.append({**restaurant, "linked": r in linked,
                                "direct_link": linked.get(r), "serviceable": result.serviceable,
                                "reason": result.reason.value if result.reason else None,
                                "message": result.message, "distance_km": result.distance_km,
                                "time_min": result.time_min})
        return {"society": society, "restaurants": restaurants,
                "counts": {"serviceable": sum(r["serviceable"] for r in restaurants),
                           "linked": len(linked)}}

    def search(self, source: str, target: str, metric: str) -> dict[str, Any]:
        source, target = identifier(source, "source"), identifier(target, "target")
        if metric not in METRIC_INDEX:
            raise ValueError("metric must be time or distance.")
        if source not in self.graph.locations or target not in self.graph.locations:
            raise ValueError("Both locations must exist in the catalog.")
        if source == target:
            raise ValueError("Choose different source and destination locations.")
        # The trace recorder patches heapq process-wide, but records only its
        # own thread (see src/tracing.py), so no service-wide lock is needed.
        d = dijkstra_with_trace(self.graph, source, target, metric)
        a = astar_with_trace(self.graph, source, target, metric, self.scales[metric])
        return {"source": source, "target": target, "metric": metric,
                "unit": METRIC_UNITS[metric], "dijkstra": d, "astar": a}

    @staticmethod
    def _fleet_run(result: tuple) -> dict[str, Any]:
        metrics, drivers, orders = result
        return {"metrics": asdict(metrics),
                "drivers": [{"id": d.driver_id,
                             "start": d.segments[0].a if d.segments else d.node,
                             "km": round(d.km, 2), "jobs": d.jobs,
                             "segs": [[s.t0, s.t1, s.a, s.b, int(s.carrying), s.order_ids]
                                      for s in d.segments]} for d in drivers],
                "orders": [{"id": o.order_id, "r": o.restaurant_id,
                            "s": o.society_id, "created": o.created, "ready": o.ready,
                            "pickup": o.pickup_time, "delivered": o.delivered_time,
                            "direct": o.direct_delivered_time, "a": o.pickup_driver,
                            "b": o.delivery_driver, "h": o.handoff_node,
                            "ht": o.handoff_time, "freed": o.minutes_freed,
                            "helper": o.helper_extra_min} for o in orders]}

    def _run_fleet(self, drivers: int, orders: int, seed: int,
                   custom: tuple[tuple[str, str, float], ...],
                   driver_nodes: tuple[str, ...] = ()) -> dict[str, Any]:
        # The shared graph is read-only here (RouteTable keeps its own trees).
        result = compare_strategies(self.graph, drivers, orders, seed,
                                    custom_orders=custom, serviceability=self.policy,
                                    driver_nodes=driver_nodes)
        drivers = len(driver_nodes) or drivers
        return {"drivers": drivers, "orders": orders, "seed": seed,
                "custom": [f"MY{i:02d}" for i in range(1, len(custom) + 1)],
                **{name: self._fleet_run(run) for name, run in result.items()}}

    def fleet(self, params: dict[str, list[str]]) -> dict[str, Any]:
        drivers = integer(params.get("drivers", ["25"])[0], "drivers", 1, 80)
        orders = integer(params.get("orders", ["40"])[0], "orders", 0, 100)
        seed = integer(params.get("seed", ["42"])[0], "seed", 0, 1_000_000)
        customs = params.get("custom", [])
        if len(customs) > MAX_CUSTOM_ORDERS:
            raise ValueError(f"Add at most {MAX_CUSTOM_ORDERS} custom orders.")
        driver_nodes = tuple(identifier(node, "driver") for node in params.get("driver", []))
        if len(driver_nodes) > MAX_FLEET_DRIVERS:
            raise ValueError(f"Choose at most {MAX_FLEET_DRIVERS} driver start places.")
        for node in driver_nodes:
            if node not in self.graph.locations:
                raise ValueError(f"Unknown driver start place: {node}.")
        if orders == 0 and not customs:
            raise ValueError("Add at least one order.")
        custom = []
        for text in customs:
            parts = text.split(",")
            if len(parts) != 3:
                raise ValueError("Custom order must be restaurant,society,minute.")
            r, s = identifier(parts[0], "restaurant"), identifier(parts[1], "society")
            minute = number(parts[2], "Custom order minute", 0, 120)
            check = self.serviceability(r, s)
            if not check["serviceable"]:
                raise ValueError(f"Custom order: {check['message']}.")
            custom.append((r, s, minute))
        return self._fleet_cache(drivers, orders, seed, tuple(custom), driver_nodes)

    def plan(self, payload: Any) -> dict[str, Any]:
        clean = scenario(payload, self.graph)
        # Closure/reroute code mutates adjacency and caches during simulation.
        # Isolating the whole graph and policy per run protects other requests.
        graph = copy.deepcopy(self.graph)
        policy = ServiceabilityPolicy(graph, self.policy.config)
        report = run_scenario(graph, ScenarioSpec.from_dict(clean), policy)
        base = report["methods"]["append"]
        improved = report["methods"]["insertion"]
        baseline_orders = {o["order_id"] for o in base["orders"] if o["status"] == "DELIVERED"}
        optimized_orders = {o["order_id"] for o in improved["orders"] if o["status"] == "DELIVERED"}
        report["comparison_valid"] = baseline_orders == optimized_orders
        # Nothing delivered -> no baseline distance -> no meaningful ratio.
        report["distance_reduction"] = (report["distance_reduction"]
                                        if report["comparison_valid"] and baseline_orders else None)
        report["rejected_orders"] = [
            {"order_id": o["order_id"], "reason": o["reason"], "message": o["message"]}
            for o in base["orders"] if o["status"] == "REJECTED"]
        return report
