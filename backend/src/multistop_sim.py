"""Multi-stop delivery simulation: naive greedy baseline vs optimiser.

Time model (minutes since start, deterministic for a given seed):

* Orders come from ``generate_orders`` (serviceable pairs only, when a
  policy is given). Creation times are spread uniformly over
  ``horizon_min``. Each order gets a preparation time drawn from
  ``prep_time_min``, so ``ready_at = created_at + prep``, and an optional
  ``deadline = created_at + max_delivery_min``.
* Every ``dispatch_interval_min`` a dispatch round runs. Drivers first
  advance along their plans to the current time (pickups, drop-offs).
  Scheduled road closures and reopenings at that time are applied next
  (``planner.close_road`` / ``reopen_road``). Newly created orders are then
  taken in, and all pending orders are offered to the planner.
* An order still pending when its deadline can no longer be met (even by
  driving straight from its restaurant) becomes FAILED. So does an order
  with no road route at all, and an order still pending when every driver
  is idle and nothing more will arrive.

The baseline (``mode="append"``) and the optimiser (``mode="insertion"``)
run on deep copies of the *same* orders and drivers, with the same
capacity, deadlines, readiness times, serviceability policy, closures and
dispatch schedule. See ``pdp.py`` for both algorithms.

Dispatch latency is the wall-clock time of one dispatch round, i.e. one
``planner.assign`` call (candidate evaluation and assignment). Rerouting
latency (one ``close_road`` call) is measured separately.
Distance-matrix precomputation (one shortest-path tree per node, done once
before the first round, like building the graph) is reported separately
and not counted in either.

Every road closed during a run is reopened when the run ends, even if the
run raises an error.
"""

from __future__ import annotations

import copy
import math
import random
import time
from dataclasses import dataclass, field

from src.cancellation import checkpoint
from src.driver import Driver, generate_drivers
from src.graph import Graph
from src.order import Order, OrderStatus, generate_orders
from src.pdp import (PickupDeliveryPlanner, PlannerConfig, RerouteReport,
                     StopKind)
from src.routing import DistanceMatrix
from src.serviceability import ServiceabilityPolicy
from src.simulation import _percentile


@dataclass(frozen=True)
class ClosureEvent:
    """Close road source -> target at ``at`` minutes, reopen at
    ``reopen_at`` (None: at the end of the run). ``source=None`` closes the
    road used by the most active plans at that moment (ties: smallest
    road), which is how the rerouting benchmark picks a disruptive road."""

    at: float
    source: str | None = None
    target: str | None = None
    reopen_at: float | None = None


@dataclass
class MultiStopConfig:
    num_drivers: int = 50
    num_orders: int = 100
    seed: int = 42
    capacity: int = 3                       # active orders per driver
    horizon_min: float = 60.0               # orders arrive within this window
    dispatch_interval_min: float = 2.0
    prep_time_min: tuple[float, float] = (5.0, 15.0)
    max_delivery_min: float | None = 60.0   # creation -> drop-off limit
    max_ride_extra_min: float | None = 20.0  # in-vehicle detour limit
    metric: str = "time"                    # optimised metric
    algorithm: str = "tree"                 # DistanceMatrix back-end
    precompute: bool = True
    closures: tuple[ClosureEvent, ...] = ()
    improve: str = "relocate"               # optimiser local improvement
    candidate_drivers: int | None = None    # optimiser candidate list (K)


@dataclass
class MultiStopResult:
    mode: str
    total_orders: int
    delivered: int
    failed: int
    rejected: int
    total_distance_km: float
    total_drive_time_min: float
    avg_delivery_min: float        # created -> delivered
    max_delivery_min: float
    late_deliveries: int           # delivered after deadline
    batched_orders: int            # on board together with another order
    drivers_used: int
    dispatch_rounds: int
    p50_latency_ms: float
    p95_latency_ms: float
    max_latency_ms: float
    orders: list[Order] = field(repr=False, default_factory=list)
    drivers: list[Driver] = field(repr=False, default_factory=list)
    reroutes: list[RerouteReport] = field(repr=False, default_factory=list)
    # (road, closed_at, reopened_at) for every closure in the run
    closure_log: list[tuple[tuple[str, str], float, float]] = field(
        repr=False, default_factory=list)
    latencies_ms: list[float] = field(repr=False, default_factory=list)


@dataclass
class Comparison:
    baseline: MultiStopResult
    optimized: MultiStopResult
    precompute_s: float

    @property
    def distance_reduction(self) -> float:
        base = self.baseline.total_distance_km
        return 0.0 if base == 0 else 1 - self.optimized.total_distance_km / base

    @property
    def same_service_level(self) -> bool:
        """The KPI is only meaningful if the optimiser delivers at least
        as many orders as the baseline."""
        return self.optimized.delivered >= self.baseline.delivered

    def kpi_passed(self, target: float = 0.15) -> bool:
        return self.same_service_level and self.distance_reduction >= target


def build_scenario(graph: Graph, config: MultiStopConfig,
                   policy: ServiceabilityPolicy | None
                   ) -> tuple[list[Order], list[Driver]]:
    """Deterministic orders (with times) and drivers for ``config``."""
    orders = generate_orders(graph, config.num_orders, config.seed,
                             config.metric, serviceability=policy)
    rng = random.Random(config.seed + 1)
    times = sorted(rng.uniform(0, config.horizon_min) for _ in orders)
    lo, hi = config.prep_time_min
    for order, created in zip(orders, times):
        order.created_at = round(created, 3)
        order.ready_at = round(created + rng.uniform(lo, hi), 3)
        if config.max_delivery_min is not None:
            order.deadline = round(created + config.max_delivery_min, 3)
    drivers = generate_drivers(graph, config.num_drivers, config.seed,
                               capacity=config.capacity)
    return orders, drivers


class DispatchRun:
    """One dispatch policy on one scenario, advanced round by round.

    ``run_until(t)`` processes every round up to time ``t``; ``finish()``
    runs to completion, reopens closed roads and returns the result.
    """

    def __init__(self, matrix: DistanceMatrix, orders: list[Order],
                 drivers: list[Driver], mode: str, config: MultiStopConfig,
                 policy: ServiceabilityPolicy | None) -> None:
        self.matrix, self.orders, self.drivers = matrix, orders, drivers
        self.mode, self.config, self.policy = mode, config, policy
        self.planner = PickupDeliveryPlanner(
            matrix, {o.order_id: o for o in orders},
            PlannerConfig(mode=mode,
                          max_ride_extra_min=config.max_ride_extra_min,
                          improve=config.improve,
                          candidate_drivers=config.candidate_drivers),
            serviceability=policy)
        self.queue = sorted(orders, key=lambda o: (o.created_at, o.order_id))
        self.arrived: list[Order] = []
        self.latencies: list[float] = []
        self.reroutes: list[RerouteReport] = []
        self.closure_log: list[tuple[tuple[str, str], float, float]] = []
        self._open_closures: dict[tuple[str, str], tuple[float, float]] = {}
        self._pending_events = sorted(config.closures, key=lambda e: e.at)
        self.now = 0.0
        self.next_index = 0
        self.done = False
        self.rounds_left = int(10 * (config.horizon_min + 600)
                               / config.dispatch_interval_min)

    # ------------------------------------------------------------------
    def busiest_road(self) -> tuple[str, str] | None:
        """Road used by the most active plans (ties: smallest road)."""
        counts: dict[tuple[str, str], int] = {}
        for driver in self.drivers:
            used: set[tuple[str, str]] = set()
            node = driver.current_node
            for stop in driver.route:
                leg = self.matrix.leg(node, stop.node)
                if leg is not None:
                    used.update(zip(leg.path, leg.path[1:]))
                node = stop.node
            for road in used:
                counts[road] = counts.get(road, 0) + 1
        if not counts:
            return None
        return min(counts, key=lambda r: (-counts[r], r))

    def close(self, source: str, target: str,
              reopen_at: float = math.inf) -> RerouteReport:
        report = self.planner.close_road(source, target, self.drivers,
                                         self.now)
        self.reroutes.append(report)
        self._open_closures[(source, target)] = (self.now, reopen_at)
        return report

    def reopen(self, source: str, target: str) -> RerouteReport:
        report = self.planner.reopen_road(source, target, self.drivers,
                                          self.now)
        closed_at, _ = self._open_closures.pop((source, target))
        self.closure_log.append(((source, target), closed_at, self.now))
        self.reroutes.append(report)
        return report

    def _apply_events(self) -> None:
        for road, (_, reopen_at) in sorted(self._open_closures.items()):
            if reopen_at <= self.now + 1e-9:
                self.reopen(*road)
        while self._pending_events and \
                self._pending_events[0].at <= self.now + 1e-9:
            event = self._pending_events.pop(0)
            road = ((event.source, event.target) if event.source
                    else self.busiest_road())
            if road is not None:
                self.close(*road, reopen_at=(event.reopen_at
                                             if event.reopen_at is not None
                                             else math.inf))

    def step(self) -> None:
        """Process the dispatch round at ``self.now``, then move the clock
        on by one interval."""
        if self.done:
            return
        checkpoint()
        if self.rounds_left <= 0:
            raise RuntimeError("simulation did not finish")
        self.rounds_left -= 1
        planner, policy, now = self.planner, self.policy, self.now
        for driver in self.drivers:
            planner.advance(driver, now)
        self._apply_events()

        queue = self.queue
        while self.next_index < len(queue) \
                and queue[self.next_index].created_at <= now:
            order = queue[self.next_index]
            self.next_index += 1
            if policy is not None and order.status is OrderStatus.PENDING:
                check = policy.check(order.restaurant_id, order.society_id)
                if not check.serviceable:
                    order.transition(OrderStatus.REJECTED)
                    order.rejection_reason = check.reason.value
                    order.rejection_message = check.message
            self.arrived.append(order)

        for order in self.arrived:
            if order.status is not OrderStatus.PENDING:
                continue
            direct = self.matrix.leg(order.restaurant_id, order.society_id)
            if direct is None:
                order.transition(OrderStatus.FAILED)
                order.failure_reason = "NO_ROAD_ROUTE"
                continue
            earliest = max(now, order.ready_at or now) + direct.time_min
            if order.deadline is not None and earliest > order.deadline:
                order.transition(OrderStatus.FAILED)
                order.failure_reason = "DEADLINE_UNREACHABLE"
        pending = [o for o in self.arrived
                   if o.status is OrderStatus.PENDING]

        if pending:
            start = time.perf_counter()
            planner.assign(pending, self.drivers, now)
            self.latencies.append(time.perf_counter() - start)

        all_arrived = self.next_index == len(queue)
        idle = all(not d.route for d in self.drivers)
        if all_arrived and idle and not self._pending_events:
            for order in self.arrived:      # nothing will ever change
                if order.status is OrderStatus.PENDING:
                    order.transition(OrderStatus.FAILED)
                    # More precise when no driver can even reach the
                    # restaurant (e.g. its only incoming road is closed).
                    reachable = any(
                        self.matrix.leg(d.current_node, order.restaurant_id)
                        is not None for d in self.drivers)
                    order.failure_reason = ("NO_FEASIBLE_DRIVER" if reachable
                                            else "PICKUP_UNREACHABLE")
            self.done = True
            return
        self.now = round(now + self.config.dispatch_interval_min, 6)

    def run_until(self, t: float) -> None:
        while not self.done and self.now <= t + 1e-9:
            self.step()

    def finish(self) -> MultiStopResult:
        try:
            while not self.done:
                self.step()
            for driver in self.drivers:
                self.planner.advance(driver, math.inf)
        finally:
            self.restore_roads()
        return _result(self)

    def restore_roads(self) -> None:
        """Reopen every road this run closed (end of simulation)."""
        for road in sorted(self._open_closures):
            closed_at, _ = self._open_closures.pop(road)
            self.closure_log.append((road, closed_at, math.inf))
            self.matrix.graph.reopen_road(*road)
            self.matrix.road_reopened()
            if self.policy is not None:
                self.policy.clear_cache()


def run_dispatch(matrix: DistanceMatrix, orders: list[Order],
                 drivers: list[Driver], mode: str, config: MultiStopConfig,
                 policy: ServiceabilityPolicy | None) -> MultiStopResult:
    """Run one dispatch policy to completion on the given orders/drivers
    (which are mutated)."""
    return DispatchRun(matrix, orders, drivers, mode, config,
                       policy).finish()


def _result(run: DispatchRun) -> MultiStopResult:
    orders, drivers = run.orders, run.drivers
    delivered = [o for o in orders if o.status is OrderStatus.DELIVERED]
    durations = [o.delivered_at - o.created_at for o in delivered]
    late = sum(1 for o in delivered
               if o.deadline is not None and o.delivered_at > o.deadline + 1e-9)

    batched: set[str] = set()
    by_driver: dict[str, list[Order]] = {}
    for order in delivered:
        by_driver.setdefault(order.assigned_driver, []).append(order)
    for group in by_driver.values():
        for a in group:
            for b in group:
                if a is not b and a.picked_up_at < b.delivered_at \
                        and b.picked_up_at < a.delivered_at:
                    batched.update((a.order_id, b.order_id))

    ms = sorted(s * 1000 for s in run.latencies)
    return MultiStopResult(
        mode=run.mode,
        total_orders=len(orders),
        delivered=len(delivered),
        failed=sum(1 for o in orders if o.status is OrderStatus.FAILED),
        rejected=sum(1 for o in orders if o.status is OrderStatus.REJECTED),
        total_distance_km=sum(d.total_distance for d in drivers),
        total_drive_time_min=sum(d.total_travel_time for d in drivers),
        avg_delivery_min=sum(durations) / len(durations) if durations else 0,
        max_delivery_min=max(durations, default=0.0),
        late_deliveries=late,
        batched_orders=len(batched),
        drivers_used=sum(1 for d in drivers if d.completed_orders),
        dispatch_rounds=len(ms),
        p50_latency_ms=_percentile(ms, 50),
        p95_latency_ms=_percentile(ms, 95),
        max_latency_ms=ms[-1] if ms else 0.0,
        orders=orders,
        drivers=drivers,
        reroutes=list(run.reroutes),
        closure_log=list(run.closure_log),
        latencies_ms=ms,
    )


def check_compliance(result: MultiStopResult,
                     policy: ServiceabilityPolicy | None,
                     max_ride_extra_min: float | None = None,
                     matrix: DistanceMatrix | None = None) -> list[str]:
    """Every delivery-constraint violation in a finished run (empty list:
    fully compliant). Deadline/ride-limit breaches of orders flagged at risk
    by a road closure are reported by ``late_deliveries`` instead."""
    problems: list[str] = []
    orders = {o.order_id: o for o in result.orders}
    visits: dict[str, list[tuple[float, StopKind, str, str]]] = {}
    for driver in result.drivers:
        load = 0
        for departure, stop in driver.stop_log:
            visits.setdefault(stop.order_id, []).append(
                (departure, stop.kind, stop.node, driver.driver_id))
            load += 1 if stop.kind is StopKind.PICKUP else -1
            if load > driver.capacity:
                problems.append(f"{driver.driver_id}: {load} orders on "
                                f"board > capacity {driver.capacity}")
        if driver.route or driver.active_orders or driver.onboard:
            problems.append(f"{driver.driver_id}: unfinished plan at end")
        for entered, u, v in driver.road_log:
            for road, closed_at, reopened_at in result.closure_log:
                if (u, v) == road and closed_at - 1e-9 <= entered \
                        < reopened_at - 1e-9:
                    problems.append(f"{driver.driver_id} entered closed "
                                    f"road {u}->{v} at {entered:.2f}")

    for order_id, order in orders.items():
        stops = visits.get(order_id, [])
        if order.status is not OrderStatus.DELIVERED:
            if any(kind is StopKind.DROPOFF for _, kind, _, _ in stops):
                problems.append(f"{order_id}: dropped off but "
                                f"{order.status.value}")
            continue
        kinds = [kind for _, kind, _, _ in stops]
        if kinds != [StopKind.PICKUP, StopKind.DROPOFF]:
            problems.append(f"{order_id}: stops {[k.value for k in kinds]}")
            continue
        (t_p, _, n_p, d_p), (t_d, _, n_d, d_d) = stops
        if n_p != order.restaurant_id or n_d != order.society_id:
            problems.append(f"{order_id}: wrong restaurant/society")
        if d_p != d_d or d_d != order.assigned_driver:
            problems.append(f"{order_id}: handled by two drivers")
        if t_p > t_d + 1e-9:
            problems.append(f"{order_id}: drop-off before pickup")
        if order.ready_at is not None and order.picked_up_at < \
                order.ready_at - 1e-9:
            problems.append(f"{order_id}: picked up before ready")
        if policy is not None and not policy.is_serviceable(
                order.restaurant_id, order.society_id):
            problems.append(f"{order_id}: unserviceable pair delivered")
        if order.risk_reason is None:
            if order.deadline is not None and \
                    order.delivered_at > order.deadline + 1e-9:
                problems.append(f"{order_id}: late without a closure")
            if max_ride_extra_min is not None:
                ride = order.delivered_at - order.picked_up_at
                limit = order.ride_limit_min
                if limit is None and matrix is not None:
                    direct = matrix.leg(order.restaurant_id, order.society_id)
                    limit = direct.time_min + max_ride_extra_min \
                        if direct else None
                if limit is None or ride > limit + 1e-6:
                    problems.append(f"{order_id}: ride time limit exceeded")
    return problems


def compare(graph: Graph, config: MultiStopConfig | None = None,
            policy: ServiceabilityPolicy | None = None,
            matrix: DistanceMatrix | None = None) -> Comparison:
    """Baseline and optimiser on identical copies of one scenario.

    ``matrix`` may be passed in to reuse precomputed shortest paths
    across calls (benchmarks); it must match the config's metric."""
    config = config or MultiStopConfig()
    orders, drivers = build_scenario(graph, config, policy)
    if matrix is None:
        matrix = DistanceMatrix(graph, config.metric, config.algorithm)
    precompute_s = 0.0
    results = []
    for mode in ("append", "insertion"):
        start = time.perf_counter()
        if config.precompute:
            matrix.precompute()     # no-op when nothing was invalidated
        precompute_s += time.perf_counter() - start
        results.append(run_dispatch(matrix, copy.deepcopy(orders),
                                    copy.deepcopy(drivers), mode, config,
                                    policy))
    return Comparison(results[0], results[1], precompute_s)
