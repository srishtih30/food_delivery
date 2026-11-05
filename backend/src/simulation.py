"""Delivery simulation engine (dispatch simulation, stage 4).

A simple, non-event-driven simulation: orders are dispatched one at a time,
in generation order, to the cheapest available driver, and each dispatch
runs the driver's full pickup -> delivery trip before the next order is
considered. This is intentionally simple - it is the foundation that
multi-stop routing and dynamic rerouting will later build on, not the final
event-driven simulator.

Existing Graph -> Existing Routing -> DispatchEngine -> DeliverySimulation
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from src.dispatch import DispatchEngine
from src.driver import Driver, generate_drivers
from src.graph import Graph
from src.heuristics import calibrate_scale
from src.order import Order, OrderStatus, generate_orders
from src.serviceability import ServiceabilityPolicy

SEED = 42  # default reproducibility seed


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty-or-empty list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


@dataclass
class SimulationMetrics:
    """Aggregate results of one simulation run."""

    total_orders: int
    delivered_orders: int
    failed_orders: int
    total_distance_km: float
    avg_distance_km: float
    total_travel_time_min: float
    avg_travel_time_min: float
    num_drivers: int
    drivers_used: int
    orders_per_driver: dict[str, int]
    driver_utilization: float
    assignment_computation_time_s: float
    total_simulation_time_s: float
    p50_assignment_latency_ms: float
    p95_assignment_latency_ms: float
    p99_assignment_latency_ms: float
    rejected_orders: int = 0   # failed the serviceability check


@dataclass
class DeliverySimulation:
    """Simulates greedy dispatch of ``num_orders`` orders across
    ``num_drivers`` drivers on the existing graph.

    Example:
        simulation = DeliverySimulation(graph, num_drivers=50, num_orders=100,
                                        seed=42)
        metrics = simulation.run()
    """

    graph: Graph
    num_drivers: int = 50
    num_orders: int = 100
    seed: int = SEED
    algorithm: str = "astar"
    metric: str = "time"
    # Serviceability gate. ``None`` + enforce -> config/serviceability.json.
    serviceability: ServiceabilityPolicy | None = None
    enforce_serviceability: bool = True
    # True -> unserviceable generated pairs become REJECTED orders (with
    # reason) instead of being skipped during generation.
    reject_unserviceable: bool = False

    drivers: list[Driver] = field(default_factory=list, init=False)
    orders: list[Order] = field(default_factory=list, init=False)
    metrics: SimulationMetrics | None = field(default=None, init=False)

    def run(self) -> SimulationMetrics:
        start = time.perf_counter()

        policy = self.serviceability
        if policy is None and self.enforce_serviceability:
            policy = ServiceabilityPolicy.default(self.graph)
        self.serviceability = policy

        self.drivers = generate_drivers(self.graph, self.num_drivers, self.seed)
        self.orders = generate_orders(self.graph, self.num_orders, self.seed,
                                      self.metric, serviceability=policy,
                                      reject_unserviceable=self.reject_unserviceable)

        heuristic_scale = (calibrate_scale(self.graph, self.metric)
                           if self.algorithm == "astar" else 0.0)
        engine = DispatchEngine(self.graph, self.algorithm, self.metric,
                                heuristic_scale, serviceability=policy)

        assignment_latencies_s: list[float] = []
        for order in self.orders:
            if order.status is not OrderStatus.PENDING:
                continue    # rejected at intake; never reaches dispatch
            t0 = time.perf_counter()
            engine.dispatch_order(order, self.drivers)
            assignment_latencies_s.append(time.perf_counter() - t0)
            engine.release_completed_drivers(self.drivers)

        total_simulation_time_s = time.perf_counter() - start
        self.metrics = self._collect_metrics(assignment_latencies_s,
                                             total_simulation_time_s)
        return self.metrics

    def _collect_metrics(self, assignment_latencies_s: list[float],
                         total_simulation_time_s: float) -> SimulationMetrics:
        delivered = [o for o in self.orders if o.status is OrderStatus.DELIVERED]
        n_delivered = len(delivered)

        total_distance = sum(o.total_distance for o in delivered)
        total_time = sum(o.total_travel_time for o in delivered)

        orders_per_driver = {d.driver_id: d.completed_orders
                             for d in self.drivers}
        drivers_used = sum(1 for count in orders_per_driver.values()
                           if count > 0)

        latencies_ms = sorted(s * 1000 for s in assignment_latencies_s)

        return SimulationMetrics(
            total_orders=len(self.orders),
            delivered_orders=n_delivered,
            failed_orders=len(self.orders) - n_delivered,
            total_distance_km=total_distance,
            avg_distance_km=total_distance / n_delivered if n_delivered else 0.0,
            total_travel_time_min=total_time,
            avg_travel_time_min=total_time / n_delivered if n_delivered else 0.0,
            num_drivers=len(self.drivers),
            drivers_used=drivers_used,
            orders_per_driver=orders_per_driver,
            driver_utilization=(drivers_used / len(self.drivers)
                                if self.drivers else 0.0),
            assignment_computation_time_s=sum(assignment_latencies_s),
            total_simulation_time_s=total_simulation_time_s,
            p50_assignment_latency_ms=_percentile(latencies_ms, 50),
            p95_assignment_latency_ms=_percentile(latencies_ms, 95),
            p99_assignment_latency_ms=_percentile(latencies_ms, 99),
            rejected_orders=sum(1 for o in self.orders
                                if o.status is OrderStatus.REJECTED),
        )
