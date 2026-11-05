"""Hand-built multi-stop scenarios (used by the web planner and the CLI).

A scenario is a user-chosen list of orders (restaurant -> society, with
optional ready time and deadline), a list of drivers (start nodes) and the
planner settings. :func:`run_scenario` runs it through the *same*
``DispatchRun`` / ``PickupDeliveryPlanner`` code as the simulation, once
with the naive greedy baseline and once with the optimiser, and returns a
JSON-friendly report.

All orders are placed at minute 0. Road closures may be scheduled at any
minute; a closure at minute 0 applies before the first plan is made.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from src.driver import Driver, generate_drivers
from src.graph import Graph
from src.multistop_sim import (ClosureEvent, DispatchRun, MultiStopConfig,
                               MultiStopResult, check_compliance)
from src.order import Order, OrderStatus
from src.routing import DistanceMatrix
from src.serviceability import ServiceabilityPolicy

ALGORITHMS = ("dijkstra", "astar", "tree")
MODE_LABELS = {"append": "Naive greedy", "insertion": "Optimised"}


@dataclass
class OrderSpec:
    restaurant: str
    society: str
    ready_at: float | None = None
    deadline: float | None = None


@dataclass
class ScenarioSpec:
    orders: list[OrderSpec]
    driver_nodes: list[str] = field(default_factory=list)
    random_drivers: int = 0           # used when driver_nodes is empty
    seed: int = 42
    capacity: int = 3
    algorithm: str = "dijkstra"       # "dijkstra", "astar" or "tree"
    metric: str = "time"
    max_ride_extra_min: float | None = 20.0
    enforce_serviceability: bool = True
    improve: str = "relocate"
    closures: list[ClosureEvent] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioSpec:
        def number(value: Any) -> float | None:
            if value is None or value == "":
                return None
            return float(value)

        orders = [OrderSpec(str(o.get("restaurant", "")).strip().upper(),
                            str(o.get("society", "")).strip().upper(),
                            number(o.get("ready_at")),
                            number(o.get("deadline")))
                  for o in data.get("orders", [])]
        closures = [ClosureEvent(float(c.get("at") or 0.0),
                                 str(c["source"]).strip().upper(),
                                 str(c["target"]).strip().upper(),
                                 number(c.get("reopen_at")))
                    for c in data.get("closures", [])]
        return cls(
            orders=orders,
            driver_nodes=[str(n).strip().upper()
                          for n in data.get("drivers", [])],
            random_drivers=int(data.get("random_drivers") or 0),
            seed=int(data.get("seed", 42)),
            capacity=int(data.get("capacity", 3)),
            algorithm=str(data.get("algorithm", "dijkstra")),
            metric=str(data.get("metric", "time")),
            max_ride_extra_min=number(data.get("max_ride_extra_min", 20.0)),
            enforce_serviceability=bool(data.get("enforce_serviceability",
                                                 True)),
            improve=str(data.get("improve", "relocate")),
            closures=closures,
        )


def _validate(graph: Graph, spec: ScenarioSpec) -> None:
    """Problems that make the request itself invalid (ValueError).
    Unserviceable or unknown order locations are *not* errors: they are
    reported per order as serviceability rejections."""
    if not spec.orders:
        raise ValueError("Add at least one order.")
    if len(spec.orders) > 200:
        raise ValueError("At most 200 orders per scenario.")
    if spec.algorithm not in ALGORITHMS:
        raise ValueError(f"Algorithm must be one of {ALGORITHMS}.")
    if spec.metric not in ("time", "distance"):
        raise ValueError("Metric must be 'time' or 'distance'.")
    if spec.improve not in ("none", "intra", "relocate"):
        raise ValueError("improve must be none, intra or relocate.")
    if not 1 <= spec.capacity <= 10:
        raise ValueError("Capacity must be between 1 and 10.")
    if not spec.driver_nodes and not 1 <= spec.random_drivers <= 100:
        raise ValueError("Add at least one driver (or 1-100 random ones).")
    for node in spec.driver_nodes:
        if node not in graph.locations:
            raise ValueError(f"Driver start '{node}' is not a known "
                             f"location.")
    for o in spec.orders:
        for value in (o.ready_at, o.deadline):
            if value is not None and (value < 0 or not math.isfinite(value)):
                raise ValueError("Ready times and deadlines must be >= 0.")
    for event in spec.closures:
        if not graph.has_edge(event.source, event.target) \
                and (event.source, event.target) not in graph.closed_roads:
            raise ValueError(f"There is no road {event.source} -> "
                             f"{event.target} to close.")
        if event.reopen_at is not None and event.reopen_at <= event.at:
            raise ValueError("A road must reopen after it closes.")


def build_orders(graph: Graph, spec: ScenarioSpec,
                 policy: ServiceabilityPolicy | None) -> list[Order]:
    orders = []
    for index, o in enumerate(spec.orders, start=1):
        order = Order(f"O{index:02d}", o.restaurant, o.society,
                      created_at=0.0, ready_at=o.ready_at,
                      deadline=o.deadline)
        known = (graph.locations.get(o.restaurant) is not None
                 and graph.locations[o.restaurant].kind == "Restaurant"
                 and graph.locations.get(o.society) is not None
                 and graph.locations[o.society].kind == "Society")
        if not known and policy is None:
            raise ValueError(f"Order {index}: '{o.restaurant}' must be a "
                             f"restaurant and '{o.society}' a society.")
        orders.append(order)
    return orders


def build_drivers(graph: Graph, spec: ScenarioSpec) -> list[Driver]:
    if spec.driver_nodes:
        return [Driver(f"D{i:02d}", node, capacity=spec.capacity)
                for i, node in enumerate(spec.driver_nodes, start=1)]
    return generate_drivers(graph, spec.random_drivers, spec.seed,
                            capacity=spec.capacity)


def run_scenario(graph: Graph, spec: ScenarioSpec,
                 policy: ServiceabilityPolicy | None,
                 matrices: dict[tuple[str, str], DistanceMatrix] | None = None
                 ) -> dict[str, Any]:
    """Plan and execute the scenario with both methods; JSON-ready."""
    _validate(graph, spec)
    if not spec.enforce_serviceability:
        policy = None
    orders = build_orders(graph, spec, policy)
    drivers = build_drivers(graph, spec)
    config = MultiStopConfig(
        num_drivers=len(drivers), num_orders=len(orders), seed=spec.seed,
        capacity=spec.capacity, horizon_min=0.0, max_delivery_min=None,
        max_ride_extra_min=spec.max_ride_extra_min, metric=spec.metric,
        algorithm=spec.algorithm, precompute=spec.algorithm == "tree",
        closures=tuple(spec.closures), improve=spec.improve)

    key = (spec.metric, spec.algorithm)
    if matrices is not None and key in matrices:
        matrix = matrices[key]
    else:
        matrix = DistanceMatrix(graph, spec.metric, spec.algorithm)
        if matrices is not None:
            matrices[key] = matrix

    report: dict[str, Any] = {"methods": {}}
    for mode in ("append", "insertion"):
        if config.precompute:
            matrix.precompute()
        run_orders, run_drivers = copy.deepcopy(orders), \
            copy.deepcopy(drivers)
        starts = {d.driver_id: d.current_node for d in run_drivers}
        result = DispatchRun(matrix, run_orders, run_drivers, mode, config,
                             policy).finish()
        report["methods"][mode] = _method_report(
            graph, result, starts,
            check_compliance(result, policy, spec.max_ride_extra_min,
                             matrix))
    base = report["methods"]["append"]["totals"]["distance_km"]
    opt = report["methods"]["insertion"]["totals"]["distance_km"]
    report["distance_reduction"] = 0.0 if base == 0 else 1 - opt / base
    report["settings"] = {
        "capacity": spec.capacity, "algorithm": spec.algorithm,
        "metric": spec.metric, "max_ride_extra_min": spec.max_ride_extra_min,
        "serviceability": policy is not None, "improve": spec.improve,
    }
    return report


def _name(graph: Graph, node: str) -> str:
    location = graph.locations.get(node)
    return location.name if location else node


def _method_report(graph: Graph, result: MultiStopResult,
                   starts: dict[str, str], violations: list[str]
                   ) -> dict[str, Any]:
    drivers = []
    for driver in result.drivers:
        if not driver.visit_log and not driver.road_log:
            continue
        drivers.append({
            "driver_id": driver.driver_id,
            "start": starts[driver.driver_id],
            "start_name": _name(graph, starts[driver.driver_id]),
            "capacity": driver.capacity,
            "distance_km": driver.total_distance,
            "drive_time_min": driver.total_travel_time,
            "path": driver.road_path,
            "stops": [{**visit, "name": _name(graph, visit["node"])}
                      for visit in driver.visit_log],
        })
    orders = []
    for order in result.orders:
        late = (order.status is OrderStatus.DELIVERED
                and order.deadline is not None
                and order.delivered_at > order.deadline + 1e-9)
        reason = (order.rejection_reason or order.failure_reason
                  or order.risk_reason)
        orders.append({
            "order_id": order.order_id,
            "restaurant": order.restaurant_id,
            "restaurant_name": _name(graph, order.restaurant_id),
            "society": order.society_id,
            "society_name": _name(graph, order.society_id),
            "status": order.status.value,
            "driver": order.assigned_driver,
            "ready_at": order.ready_at,
            "deadline": order.deadline,
            "picked_up_at": order.picked_up_at,
            "delivered_at": order.delivered_at,
            "late": late,
            "reason": reason,
            "message": order.rejection_message,
            "reassignments": order.reassignments,
        })
    return {
        "label": MODE_LABELS[result.mode],
        "totals": {
            "delivered": result.delivered,
            "failed": result.failed,
            "rejected": result.rejected,
            "late": result.late_deliveries,
            "batched": result.batched_orders,
            "drivers_used": result.drivers_used,
            "distance_km": result.total_distance_km,
            "drive_time_min": result.total_drive_time_min,
            "planning_ms": sum(result.latencies_ms),
        },
        "drivers": drivers,
        "orders": orders,
        "reroutes": [{
            "action": r.action, "road": list(r.road), "at": r.at,
            "affected_drivers": r.affected_drivers,
            "reanchored": r.reanchored,
            "released_orders": r.released_orders,
            "failed_orders": r.failed_orders,
            "at_risk_orders": r.at_risk_orders,
            "latency_ms": r.latency_s * 1000,
        } for r in result.reroutes],
        "violations": violations,
    }
