"""Multi-stop pickup-and-delivery routing (one engine for every case).

A driver's plan is an ordered list of :class:`Stop` s. Every stop belongs to
exactly one order: a PICKUP at that order's restaurant or a DROPOFF at that
order's society. The same engine covers:

* one society, several restaurants   (R1, R2 -> S1)
* one restaurant, several societies  (R1 -> S1, S2, S3)
* several restaurants and societies  (R1 -> S1, R2 -> S2, ...)

Costs between consecutive stops come from :class:`routing.DistanceMatrix`,
which uses the existing graph's shortest paths. A plan is expanded into real
road nodes only for display (:func:`expand_route`).

Feasibility of a plan (:meth:`PickupDeliveryPlanner.evaluate`)
---------------------------------------------------------------
* every stop is reachable from the previous one by a real directed route
* each order: exactly one pickup, at its own restaurant, before exactly one
  drop-off, at its own society (orders already on board: drop-off only)
* the driver never holds more active orders than ``driver.capacity``
* a pickup cannot leave before the order's ``ready_at`` (the driver waits)
* drop-off arrival <= the order's ``deadline`` (when one is set)
* optional ``max_ride_extra_min``: time between pickup and drop-off is at
  most the direct restaurant -> society time plus this many minutes

Assignment (:meth:`PickupDeliveryPlanner.assign`)
-------------------------------------------------
``mode="insertion"`` (the optimiser): cheapest insertion. Each pending
order's pickup and drop-off are tried at every position pair (i <= j) of
every driver's current plan, after the stop the driver is already heading
to. Infeasible positions are discarded, and the insertion with the
smallest added travel cost is applied. This repeats until no pending order
has a feasible insertion. Ties are broken by (order ID, driver ID,
positions), so results are deterministic. Work per order is
O(drivers x stops^2 x stops), with stops <= 2 x capacity; nothing
enumerates permutations.

``mode="append"`` (the naive greedy baseline): orders are taken one at a
time in arrival order. Each order is appended to the end of a plan
(pickup, then drop-off) of the feasible driver with the smallest added
cost. There is no reordering and no interleaving. Constraints, drivers,
capacity and orders are identical to the optimiser's.

Neither mode claims a globally optimal vehicle-routing solution.
Cheapest insertion is a heuristic.

Commitments: an assigned order stays with its driver (no reassignment).
The stop a driver is currently driving to is never moved. New orders can
only be inserted where every order already in the plan stays feasible.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from typing import TYPE_CHECKING

from src.cancellation import checkpoint
from src.driver import Driver, DriverStatus
from src.order import Order, OrderStatus
from src.routing import DistanceMatrix

if TYPE_CHECKING:
    from src.serviceability import ServiceabilityPolicy

_EPS = 1e-9


class StopKind(str, Enum):
    PICKUP = "PICKUP"
    DROPOFF = "DROPOFF"


@dataclass(frozen=True)
class Stop:
    kind: StopKind
    order_id: str
    node: str

    def __str__(self) -> str:
        return f"{'P' if self.kind is StopKind.PICKUP else 'D'}" \
               f"({self.order_id}@{self.node})"


def pickup(order: Order) -> Stop:
    return Stop(StopKind.PICKUP, order.order_id, order.restaurant_id)


def dropoff(order: Order) -> Stop:
    return Stop(StopKind.DROPOFF, order.order_id, order.society_id)


@dataclass(frozen=True)
class PlannerConfig:
    mode: str = "insertion"                 # "insertion" or "append"
    max_ride_extra_min: float | None = None  # in-vehicle detour limit
    # Bounded local improvement after each insertion round (optimiser only):
    #   "none"     - cheapest insertion only
    #   "intra"    - re-insert each not-yet-picked-up order within its own
    #                driver's plan if that is strictly cheaper
    #   "relocate" - as "intra", but the order may also move to another
    #                driver (an explicit, counted reassignment; only orders
    #                not yet picked up, whose pickup is not being driven to)
    improve: str = "none"
    # Candidate list (bounded search): each order is only offered to the K
    # drivers whose position or planned stops are closest (shortest route)
    # to its restaurant. None = every driver (exhaustive).
    candidate_drivers: int | None = None


class _State:
    """Running state while simulating a plan stop by stop."""

    __slots__ = ("node", "t", "cost", "km", "minutes", "load", "active",
                 "picked_at", "dropped")

    def __init__(self, node: str, t: float, load: int) -> None:
        self.node, self.t, self.load = node, t, load
        self.cost = self.km = self.minutes = 0.0
        self.active: set[str] = set()
        self.picked_at: dict[str, float] = {}
        self.dropped: set[str] = set()

    def copy(self) -> _State:
        other = _State.__new__(_State)
        other.node, other.t, other.load = self.node, self.t, self.load
        other.cost, other.km, other.minutes = self.cost, self.km, self.minutes
        other.active = set(self.active)
        other.picked_at = dict(self.picked_at)
        other.dropped = set(self.dropped)
        return other


@dataclass(frozen=True)
class RouteEvaluation:
    cost: float             # optimised metric over the remaining plan
    distance_km: float
    drive_time_min: float
    finish_time: float      # clock when the last stop is completed


@dataclass(frozen=True)
class Insertion:
    order_id: str
    driver_id: str
    pickup_index: int        # positions in the *new* route
    dropoff_index: int
    added_cost: float
    base_route: tuple[Stop, ...]
    route: tuple[Stop, ...]

    def key(self) -> tuple:
        return self._key

    @cached_property
    def _key(self) -> tuple:
        return (round(self.added_cost, 9), self.order_id, self.driver_id,
                self.pickup_index, self.dropoff_index)


@dataclass
class RerouteReport:
    """What one road closure (or reopening) did to the active plans."""

    road: tuple[str, str]
    action: str                                  # "close" or "reopen"
    at: float
    affected_drivers: list[str] = field(default_factory=list)
    reanchored: dict[str, str] = field(default_factory=dict)
    released_orders: dict[str, str] = field(default_factory=dict)
    failed_orders: dict[str, str] = field(default_factory=dict)
    at_risk_orders: dict[str, str] = field(default_factory=dict)
    latency_s: float = 0.0


class AssignmentError(ValueError):
    """An assignment would break a hard rule (duplicate, capacity, ...)."""


class PickupDeliveryPlanner:
    """Plans, assigns and executes multi-stop pickup-and-delivery routes."""

    MODES = ("insertion", "append")

    def __init__(self, matrix: DistanceMatrix, orders: dict[str, Order],
                 config: PlannerConfig | None = None,
                 serviceability: ServiceabilityPolicy | None = None) -> None:
        self.matrix = matrix
        self.orders = orders
        self.config = config or PlannerConfig()
        if self.config.mode not in self.MODES:
            raise ValueError(f"Unknown planner mode '{self.config.mode}'.")
        self.serviceability = serviceability

    # ------------------------------------------------------------------
    # Feasibility
    # ------------------------------------------------------------------
    @staticmethod
    def _start(driver: Driver, now: float) -> float:
        # Idle drivers start now; a driver with a plan already left its
        # last stop at driver.clock.
        return driver.clock if driver.route else max(driver.clock, now)

    def evaluate(self, driver: Driver, stops: list[Stop] | tuple[Stop, ...],
                 now: float, cost_limit: float = math.inf
                 ) -> RouteEvaluation | None:
        """Simulate ``stops`` from the driver's position; ``None`` if any
        constraint is violated, a leg is unreachable, or the cost exceeds
        ``cost_limit`` (used for pruning: costs only grow along a route)."""
        state = self._begin(driver, now)
        for stop in stops:
            if not self._visit(state, stop, driver, cost_limit):
                return None
        return self._finish(state, driver)

    # The three steps of ``evaluate``, split so that insertion search can
    # reuse the state after a route prefix instead of re-simulating it.
    def _begin(self, driver: Driver, now: float) -> _State:
        return _State(driver.current_node, self._start(driver, now),
                      len(driver.onboard))

    def _visit(self, s: _State, stop: Stop, driver: Driver,
               cost_limit: float = math.inf) -> bool:
        """Advance state ``s`` through ``stop``; False if infeasible."""
        order = self.orders.get(stop.order_id)
        if order is None:
            return False
        leg = self.matrix.leg(s.node, stop.node)
        if leg is None:
            return False
        t = s.t + leg.time_min
        s.cost += leg.cost
        if s.cost > cost_limit + _EPS:
            return False
        s.km += leg.distance_km
        s.minutes += leg.time_min
        s.active.add(stop.order_id)

        if stop.kind is StopKind.PICKUP:
            if (stop.node != order.restaurant_id
                    or stop.order_id in s.picked_at
                    or stop.order_id in driver.onboard
                    or stop.order_id in s.dropped):
                return False
            if order.ready_at is not None and order.ready_at > t:
                t = order.ready_at
            s.picked_at[stop.order_id] = t
            s.load += 1
            if s.load > driver.capacity:
                return False
        else:
            if stop.node != order.society_id or stop.order_id in s.dropped:
                return False
            if stop.order_id in s.picked_at:
                pickup_time = s.picked_at.pop(stop.order_id)
            elif stop.order_id in driver.onboard:
                pickup_time = order.picked_up_at or 0.0
            else:
                return False                    # drop-off before pickup
            if order.deadline is not None and t > order.deadline + _EPS:
                return False
            if self.config.max_ride_extra_min is not None:
                limit = self.ride_limit(order)
                if limit is None or t - pickup_time > limit + _EPS:
                    return False
            s.dropped.add(stop.order_id)
            s.load -= 1
        s.t = t
        s.node = stop.node
        return True

    @staticmethod
    def _finish(s: _State, driver: Driver) -> RouteEvaluation | None:
        # Everything picked up (or already on board) must be delivered, and
        # the driver may not hold more active orders than its capacity.
        if s.picked_at or not driver.onboard <= s.dropped:
            return None
        if len(s.active.union(driver.active_orders)) > driver.capacity:
            return None
        return RouteEvaluation(s.cost, s.km, s.minutes, s.t)

    def ride_limit(self, order: Order) -> float | None:
        """Maximum pickup -> drop-off time for ``order``: direct route time
        plus ``max_ride_extra_min``. It is fixed when the order is assigned
        (``order.ride_limit_min``), so a later closure or reopening does not
        silently change the promise. ``None``: no limit configured, or no
        direct route exists (the order cannot be planned)."""
        extra = self.config.max_ride_extra_min
        if extra is None:
            return None
        if order.ride_limit_min is not None:
            return order.ride_limit_min
        direct = self.matrix.leg(order.restaurant_id, order.society_id)
        return None if direct is None else direct.time_min + extra

    @staticmethod
    def locked_stops(driver: Driver, now: float) -> int:
        """Stops at the front of the plan that may not be moved: the stop
        the driver is already driving to (or waiting at). A driver whose
        clock equals ``now`` is still at its node, so nothing is locked."""
        return 1 if driver.route and driver.clock < now - _EPS else 0

    def is_eligible(self, order: Order) -> bool:
        """Order may be planned: pending, unassigned and serviceable."""
        if order.status is not OrderStatus.PENDING \
                or order.assigned_driver is not None:
            return False
        if self.serviceability is not None and not \
                self.serviceability.is_serviceable(order.restaurant_id,
                                                   order.society_id):
            return False
        return True

    def best_insertion(self, order: Order, driver: Driver, now: float,
                       base: RouteEvaluation | None = None
                       ) -> Insertion | None:
        """Cheapest feasible way to add ``order`` to ``driver``'s plan."""
        if not self.is_eligible(order) or driver.remaining_capacity <= 0:
            return None
        route = driver.route
        if base is None:
            base = self.evaluate(driver, route, now)
        if base is None:
            return None
        found = self._cheapest_positions(order, driver, route, base.cost, now)
        if found is None:
            return None
        added, i, j, candidate = found
        return Insertion(order.order_id, driver.driver_id, i, j + 1, added,
                         tuple(route), candidate)

    def _cheapest_positions(self, order: Order, driver: Driver,
                            route: list[Stop] | tuple[Stop, ...],
                            base_cost: float, now: float,
                            max_added: float = math.inf
                            ) -> tuple[float, int, int, tuple[Stop, ...]] \
            | None:
        """Cheapest feasible (added_cost, i, j, new_route) for inserting
        ``order`` into ``route`` (pickup at i, drop-off before old index
        j), or None. Only added costs <= ``max_added`` are considered."""
        p, d = pickup(order), dropoff(order)
        n = len(route)
        locked = self.locked_stops(driver, now) if route else 0
        first_i = n if self.config.mode == "append" else locked

        # State after each prefix route[:k], computed once (no cost limit).
        prefixes = [self._begin(driver, now)]
        for k in range(first_i):
            state = prefixes[-1].copy()
            if not self._visit(state, route[k], driver):
                return None
            prefixes.append(state)
        prefix = prefixes[-1]

        best: tuple[float, int, int] | None = None
        limit = base_cost + max_added
        # Positions are tried in (i, j) order; a candidate costing more than
        # the best so far cannot win (ties go to the earlier pair), so its
        # evaluation stops early. If the plan is already infeasible after
        # route[i:j] with the pickup inserted, every larger j is too.
        for i in range(first_i, n + 1):
            if i > first_i:
                prefix = prefix.copy()
                if not self._visit(prefix, route[i - 1], driver):
                    break
            middle = prefix.copy()
            if not self._visit(middle, p, driver, limit):
                continue
            last_j = n if self.config.mode == "insertion" else i
            for j in range(i, last_j + 1):
                if j > i and not self._visit(middle, route[j - 1], driver,
                                             limit):
                    break
                state = middle.copy()
                if not self._visit(state, d, driver, limit):
                    continue
                ok = True
                for stop in route[j:]:
                    if not self._visit(state, stop, driver, limit):
                        ok = False
                        break
                if not ok:
                    continue
                evaluation = self._finish(state, driver)
                if evaluation is None:
                    continue
                added = evaluation.cost - base_cost
                if best is None or round(added, 9) < round(best[0], 9):
                    best = (added, i, j)
                    limit = evaluation.cost
        if best is None:
            return None
        added, i, j = best
        return added, i, j, (*route[:i], p, *route[i:j], d, *route[j:])

    # ------------------------------------------------------------------
    # Bounded local improvement
    # ------------------------------------------------------------------
    def improve(self, drivers: list[Driver], now: float) -> int:
        """One deterministic pass of remove-and-reinsert over every order
        that is assigned but not yet picked up (and whose pickup is not the
        stop being driven to). A move is applied only if it lowers the total
        planned cost by more than 1e-6 and every affected plan stays
        feasible. Returns the number of moves. Bounded: one pass, each
        order tried once, at most O(drivers x stops^3) work per order."""
        mode = self.config.improve
        if mode == "none" or self.config.mode != "insertion":
            return 0
        if mode not in ("intra", "relocate"):
            raise ValueError(f"Unknown improve mode '{mode}'.")
        drivers = sorted(drivers, key=lambda d: d.driver_id)
        by_id = {d.driver_id: d for d in drivers}
        bases = {d.driver_id: self.evaluate(d, d.route, now) for d in drivers}
        candidates = sorted(
            (o for o in self.orders.values()
             if o.status is OrderStatus.ASSIGNED
             and o.assigned_driver in by_id),
            key=lambda o: o.order_id)

        moves = 0
        for order in candidates:
            checkpoint()
            owner = by_id[order.assigned_driver]
            base_owner = bases[owner.driver_id]
            if base_owner is None:
                continue
            first = next(k for k, s in enumerate(owner.route)
                         if s.order_id == order.order_id)
            if first < self.locked_stops(owner, now):
                continue                    # its pickup is being driven to
            reduced = tuple(s for s in owner.route
                            if s.order_id != order.order_id)
            reduced_eval = self.evaluate(owner, reduced, now)
            if reduced_eval is None:
                continue
            saving = base_owner.cost - reduced_eval.cost

            best = None     # (gain, driver_id, i, j, route)
            if mode == "relocate":
                targets = self.candidates(order, drivers)[0]
                if owner not in targets:
                    targets.append(owner)
            else:
                targets = [owner]
            for target in targets:
                if target is owner:
                    route, base_cost = reduced, reduced_eval.cost
                else:
                    if target.remaining_capacity <= 0 \
                            or bases[target.driver_id] is None:
                        continue
                    route = target.route
                    base_cost = bases[target.driver_id].cost
                found = self._cheapest_positions(order, target, route,
                                                 base_cost, now, saving)
                if found is None:
                    continue
                added, i, j, new_route = found
                gain = saving - added
                key = (-round(gain, 9), target.driver_id, i, j)
                if gain > 1e-6 and (best is None or key < best[0]):
                    best = (key, target, new_route)
            if best is None:
                continue

            _, target, new_route = best
            if target is owner:
                owner.route = list(new_route)
            else:
                owner.route = list(reduced)
                owner.active_orders.remove(order.order_id)
                if not target.route:
                    target.clock = max(target.clock, now)
                target.route = list(new_route)
                target.active_orders.append(order.order_id)
                order.assigned_driver = target.driver_id
                order.reassignments += 1
                _update_status(owner)
                _update_status(target)
                validate_route(target, self.orders)
                bases[target.driver_id] = self.evaluate(target, target.route,
                                                        now)
            validate_route(owner, self.orders)
            bases[owner.driver_id] = self.evaluate(owner, owner.route, now)
            moves += 1
        return moves

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------
    def apply(self, insertion: Insertion, driver: Driver, now: float) -> None:
        """Commit an insertion. Raises :class:`AssignmentError` if the order
        is already assigned, the driver is full or the plan changed."""
        order = self.orders[insertion.order_id]
        if driver.driver_id != insertion.driver_id:
            raise AssignmentError("insertion was planned for another driver")
        if order.status is not OrderStatus.PENDING \
                or order.assigned_driver is not None:
            raise AssignmentError(
                f"{order.order_id} is already assigned to "
                f"{order.assigned_driver} ({order.status.value})")
        if driver.remaining_capacity <= 0:
            raise AssignmentError(f"{driver.driver_id} is at capacity")
        if tuple(driver.route) != insertion.base_route:
            raise AssignmentError(f"{driver.driver_id}'s plan changed")
        locked = self.locked_stops(driver, now)
        if insertion.route[:locked] != tuple(driver.route[:locked]):
            raise AssignmentError("insertion moves the stop the driver is "
                                  "already driving to")
        if self.evaluate(driver, insertion.route, now) is None:
            raise AssignmentError("insertion is no longer feasible")

        if not driver.route:
            driver.clock = max(driver.clock, now)
        driver.route = list(insertion.route)
        driver.active_orders.append(order.order_id)
        order.transition(OrderStatus.ASSIGNED)
        order.assigned_driver = driver.driver_id
        order.assigned_at = now
        if order.ride_limit_min is None:
            order.ride_limit_min = self.ride_limit(order)
        _update_status(driver)
        validate_route(driver, self.orders)

    def assign(self, pending: list[Order], drivers: list[Driver],
               now: float) -> list[Insertion]:
        """Assign as many ``pending`` orders as feasible; returns the applied
        insertions in the order they were made."""
        candidates = sorted((o for o in pending if self.is_eligible(o)),
                            key=lambda o: (o.created_at, o.order_id))
        drivers = sorted(drivers, key=lambda d: d.driver_id)
        if self.config.mode == "append":
            return self._assign_fifo(candidates, drivers, now)
        applied = self._assign_cheapest(candidates, drivers, now)
        if self.config.improve != "none":
            self.improve(drivers, now)
        return applied

    def _assign_fifo(self, orders: list[Order], drivers: list[Driver],
                     now: float) -> list[Insertion]:
        applied = []
        by_id = {d.driver_id: d for d in drivers}
        for order in orders:
            options = [ins for d in drivers
                       if (ins := self.best_insertion(order, d, now))]
            if options:
                best = min(options, key=Insertion.key)
                self.apply(best, by_id[best.driver_id], now)
                applied.append(best)
        return applied

    def proximity(self, driver: Driver, node: str) -> float:
        """Shortest route cost from the driver's position or any of its
        planned stops to ``node``."""
        cost = self.matrix.cost(driver.current_node, node)
        for stop in driver.route:
            cost = min(cost, self.matrix.cost(stop.node, node))
        return cost

    def candidates(self, order: Order, drivers: list[Driver]
                   ) -> tuple[list[Driver], float]:
        """The K drivers closest to the order's restaurant (ties: driver
        ID) and the K-th proximity; all drivers when no K is configured."""
        k = self.config.candidate_drivers
        if k is None or k >= len(drivers):
            return list(drivers), math.inf
        ranked = sorted(drivers, key=lambda d: (
            self.proximity(d, order.restaurant_id), d.driver_id))[:k]
        return ranked, self.proximity(ranked[-1], order.restaurant_id)

    def _assign_cheapest(self, orders: list[Order], drivers: list[Driver],
                         now: float) -> list[Insertion]:
        by_id = {d.driver_id: d for d in drivers}
        bases = {d.driver_id: self.evaluate(d, d.route, now) for d in drivers}
        best: dict[tuple[str, str], Insertion] = {}
        options: dict[str, set[str]] = {o.order_id: set() for o in orders}
        heap: list[tuple[tuple, int, Insertion]] = []  # stale ones skipped
        pushed = 0

        def refresh(order: Order, driver: Driver) -> None:
            ins = self.best_insertion(order, driver, now,
                                      bases[driver.driver_id])
            key = (order.order_id, driver.driver_id)
            if ins is None:
                best.pop(key, None)
                options[order.order_id].discard(driver.driver_id)
            else:
                best[key] = ins
                options[order.order_id].add(driver.driver_id)
                nonlocal pushed
                pushed += 1
                heapq.heappush(heap, (ins.key(), pushed, ins))

        allowed: dict[str, set[str]] = {}
        threshold: dict[str, float] = {}
        for order in orders:
            chosen, threshold[order.order_id] = self.candidates(order,
                                                                drivers)
            allowed[order.order_id] = {d.driver_id for d in chosen}
            for driver in chosen:
                refresh(order, driver)

        applied = []
        remaining = {o.order_id: o for o in orders}
        while heap:
            _, _, choice = heapq.heappop(heap)
            checkpoint()
            if best.get((choice.order_id, choice.driver_id)) is not choice:
                continue                         # superseded or removed
            driver = by_id[choice.driver_id]
            self.apply(choice, driver, now)
            applied.append(choice)
            del remaining[choice.order_id]
            for driver_id in options.pop(choice.order_id):
                del best[(choice.order_id, driver_id)]
            # Only the changed driver's options need recomputing. Its new
            # stops may bring it within an order's candidate radius.
            bases[driver.driver_id] = self.evaluate(driver, driver.route, now)
            for order in remaining.values():
                ids = allowed[order.order_id]
                if driver.driver_id not in ids:
                    if self.proximity(driver, order.restaurant_id) > \
                            threshold[order.order_id]:
                        continue
                    ids.add(driver.driver_id)
                refresh(order, driver)
        return applied

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def advance(self, driver: Driver, until: float) -> None:
        """Complete every stop the driver can finish by time ``until``."""
        while driver.route:
            stop = driver.route[0]
            order = self.orders[stop.order_id]
            leg = self.matrix.leg(driver.current_node, stop.node)
            if leg is None:
                raise RuntimeError(f"{driver.driver_id}: no route "
                                   f"{driver.current_node} -> {stop.node}")
            arrival = driver.clock + leg.time_min
            departure = arrival
            if stop.kind is StopKind.PICKUP and order.ready_at is not None:
                departure = max(arrival, order.ready_at)
            if departure > until:
                break

            if order.assigned_driver != driver.driver_id:
                raise ValueError(f"{driver.driver_id} holds a stop of "
                                 f"{order.order_id}, assigned to "
                                 f"{order.assigned_driver}")
            if stop.kind is StopKind.PICKUP:
                if stop.node != order.restaurant_id:
                    raise ValueError(f"{order.order_id}: pickup at wrong "
                                     f"restaurant {stop.node}")
                order.transition(OrderStatus.PICKED_UP)
                order.picked_up_at = departure
                driver.onboard.add(order.order_id)
            else:
                if stop.node != order.society_id:
                    raise ValueError(f"{order.order_id}: drop-off at wrong "
                                     f"society {stop.node}, expected "
                                     f"{order.society_id}")
                if order.order_id not in driver.onboard:
                    raise ValueError(f"{order.order_id}: drop-off before "
                                     f"pickup")
                order.transition(OrderStatus.DELIVERED)
                order.delivered_at = arrival
                driver.onboard.discard(order.order_id)
                driver.active_orders.remove(order.order_id)
                driver.completed_orders += 1

            driver.route.pop(0)
            self._drive(driver, leg.path, driver.clock)
            driver.current_node = stop.node
            driver.clock = departure
            driver.stop_log.append((departure, stop))
            driver.visit_log.append({
                "order_id": stop.order_id, "kind": stop.kind.value,
                "node": stop.node, "arrival": arrival,
                "departure": departure,
                "cum_km": driver.total_distance,
                "cum_min": driver.total_travel_time,
            })
        _update_status(driver)

    def _road(self, source: str, target: str) -> tuple[float, float]:
        graph = self.matrix.graph
        weights = graph.adjacency.get(source, {}).get(target)
        return weights if weights is not None \
            else graph.closed_roads[(source, target)]

    def _drive(self, driver: Driver, path: tuple[str, ...] | list[str],
               start: float) -> float:
        """Record driving ``path`` from ``start``; returns arrival time."""
        t = start
        if not driver.road_path:
            driver.road_path.append(path[0])
        for u, v in zip(path, path[1:]):
            km, minutes = self._road(u, v)
            driver.road_log.append((t, u, v))
            driver.road_path.append(v)
            driver.total_distance += km
            driver.total_travel_time += minutes
            t += minutes
        return t

    # ------------------------------------------------------------------
    # Road closures and rerouting
    # ------------------------------------------------------------------
    def _reanchor(self, driver: Driver, now: float) -> str | None:
        """Commitment rule for a driver already travelling to route[0]:
        it finishes the road segment it is on (no teleporting, no turning
        back mid-road) and its position becomes that segment's end node, at
        the time it gets there. route[0] itself is not changed. Returns the
        new node, or None if the driver is not travelling."""
        if not driver.route or driver.clock >= now - _EPS:
            return None
        leg = self.matrix.leg(driver.current_node, driver.route[0].node)
        if leg is None or len(leg.path) < 2:
            return None
        t, k = driver.clock, 0
        while k < len(leg.path) - 1 and t < now - _EPS:
            t += self._road(leg.path[k], leg.path[k + 1])[1]
            k += 1
        if k == 0:
            return None
        self._drive(driver, leg.path[:k + 1], driver.clock)
        driver.current_node = leg.path[k]
        driver.clock = t
        return driver.current_node

    def _uses_road(self, driver: Driver, road: tuple[str, str]) -> bool:
        node = driver.current_node
        for stop in driver.route:
            leg = self.matrix.leg(node, stop.node)
            if leg is not None and any(
                    (u, v) == road for u, v in zip(leg.path, leg.path[1:])):
                return True
            node = stop.node
        return False

    def close_road(self, source: str, target: str, drivers: list[Driver],
                   now: float) -> RerouteReport:
        """Block road source -> target at time ``now`` and repair every
        active plan that used it (see :meth:`_repair`)."""
        start = time.perf_counter()
        graph = self.matrix.graph
        if not graph.has_edge(source, target):
            raise ValueError(f"No open road {source} -> {target}.")
        road = (source, target)
        report = RerouteReport(road, "close", now)
        drivers = sorted(drivers, key=lambda d: d.driver_id)
        for driver in drivers:
            if driver.route and self._uses_road(driver, road):
                report.affected_drivers.append(driver.driver_id)
                node = self._reanchor(driver, now)
                if node is not None:
                    report.reanchored[driver.driver_id] = node

        graph.close_road(source, target)
        self.matrix.road_closed(source, target)
        if self.serviceability is not None:
            self.serviceability.clear_cache()
        for driver in drivers:
            if driver.driver_id in report.affected_drivers:
                self._repair(driver, now, report)
        report.latency_s = time.perf_counter() - start
        return report

    def reopen_road(self, source: str, target: str, drivers: list[Driver],
                    now: float) -> RerouteReport:
        """Restore a closed road. Every travelling driver first finishes
        its current segment (cached paths may change), then routes use the
        restored road where it is shorter."""
        start = time.perf_counter()
        road = (source, target)
        report = RerouteReport(road, "reopen", now)
        for driver in sorted(drivers, key=lambda d: d.driver_id):
            node = self._reanchor(driver, now)
            if node is not None:
                report.reanchored[driver.driver_id] = node
        self.matrix.graph.reopen_road(source, target)
        self.matrix.road_reopened()
        if self.serviceability is not None:
            self.serviceability.clear_cache()
        report.latency_s = time.perf_counter() - start
        return report

    def _repair(self, driver: Driver, now: float,
                report: RerouteReport) -> None:
        """Keep the driver's stop sequence but drop what is now
        unreachable; road paths between stops are recomputed by the
        routing engine (the closed road is no longer in the graph).

        * unreachable stop of an order not yet picked up: the order is
          released to PENDING (``ROAD_CLOSED_UNREACHABLE``) so the
          dispatcher can offer it to another driver;
        * unreachable drop-off of an order on board: FAILED
          (``DROPOFF_UNREACHABLE``) - never marked delivered;
        * still reachable but now violating a deadline or ride limit:
          reported as at risk; the delivery continues.
        """
        changed = True
        while changed:
            changed = False
            node = driver.current_node
            for stop in driver.route:
                if self.matrix.leg(node, stop.node) is None:
                    self._drop_order(driver, stop.order_id, report)
                    changed = True
                    break
                node = stop.node
        _update_status(driver)
        if driver.route and self.evaluate(driver, driver.route, now) is None:
            for order_id, reason in self.explain(driver, driver.route, now):
                report.at_risk_orders[order_id] = reason
                self.orders[order_id].risk_reason = reason
        validate_route(driver, self.orders)

    def _drop_order(self, driver: Driver, order_id: str,
                    report: RerouteReport) -> None:
        order = self.orders[order_id]
        driver.route = [s for s in driver.route if s.order_id != order_id]
        driver.active_orders.remove(order_id)
        if order_id in driver.onboard:
            driver.onboard.discard(order_id)
            order.transition(OrderStatus.FAILED)
            order.failure_reason = "DROPOFF_UNREACHABLE"
            report.failed_orders[order_id] = order.failure_reason
        else:
            order.release()
            report.released_orders[order_id] = "ROAD_CLOSED_UNREACHABLE"

    def explain(self, driver: Driver, stops: list[Stop] | tuple[Stop, ...],
                now: float) -> list[tuple[str, str]]:
        """Which orders in ``stops`` violate a time constraint, and why
        (same rules as :meth:`evaluate`, but reporting instead of
        rejecting)."""
        extra = self.config.max_ride_extra_min
        node, t = driver.current_node, self._start(driver, now)
        picked_at: dict[str, float] = {}
        problems: list[tuple[str, str]] = []
        for stop in stops:
            order = self.orders[stop.order_id]
            leg = self.matrix.leg(node, stop.node)
            if leg is None:
                problems.append((stop.order_id, "UNREACHABLE"))
                return problems
            t += leg.time_min
            if stop.kind is StopKind.PICKUP:
                if order.ready_at is not None:
                    t = max(t, order.ready_at)
                picked_at[stop.order_id] = t
            else:
                pickup_time = picked_at.pop(stop.order_id,
                                            order.picked_up_at or 0.0)
                if order.deadline is not None and t > order.deadline + _EPS:
                    problems.append((stop.order_id, "DEADLINE_AT_RISK"))
                elif extra is not None:
                    limit = self.ride_limit(order)
                    if limit is None or t - pickup_time > limit + _EPS:
                        problems.append((stop.order_id,
                                         "RIDE_TIME_LIMIT_AT_RISK"))
            node = stop.node
        return problems


def _update_status(driver: Driver) -> None:
    if not driver.route:
        driver.status = DriverStatus.AVAILABLE
    elif driver.route[0].kind is StopKind.PICKUP:
        driver.status = DriverStatus.TO_PICKUP
    else:
        driver.status = DriverStatus.TO_DROPOFF


def validate_route(driver: Driver, orders: dict[str, Order]) -> None:
    """Raise ValueError unless the driver's plan is structurally valid:
    every active order appears exactly as required (pickup at its own
    restaurant before drop-off at its own society; on-board orders only a
    drop-off), no foreign orders, and capacity is respected."""
    active = set(driver.active_orders)
    if len(active) != len(driver.active_orders):
        raise ValueError(f"{driver.driver_id}: duplicate active order")
    if len(active) > driver.capacity:
        raise ValueError(f"{driver.driver_id}: {len(active)} active orders "
                         f"> capacity {driver.capacity}")
    if not driver.onboard <= active:
        raise ValueError(f"{driver.driver_id}: on-board order not active")

    seen: dict[str, list[StopKind]] = {}
    for stop in driver.route:
        order = orders.get(stop.order_id)
        if order is None or stop.order_id not in active:
            raise ValueError(f"{driver.driver_id}: stop for foreign order "
                             f"{stop.order_id}")
        if order.assigned_driver != driver.driver_id:
            raise ValueError(f"{stop.order_id} is assigned to "
                             f"{order.assigned_driver}, not "
                             f"{driver.driver_id}")
        expected = (order.restaurant_id if stop.kind is StopKind.PICKUP
                    else order.society_id)
        if stop.node != expected:
            raise ValueError(f"{stop.order_id}: {stop.kind.value} at "
                             f"{stop.node}, expected {expected}")
        seen.setdefault(stop.order_id, []).append(stop.kind)

    for order_id in active:
        kinds = seen.get(order_id, [])
        required = ([StopKind.DROPOFF] if order_id in driver.onboard
                    else [StopKind.PICKUP, StopKind.DROPOFF])
        if kinds != required:
            raise ValueError(f"{order_id}: stops {[k.value for k in kinds]},"
                             f" expected {[k.value for k in required]}")


def expand_route(matrix: DistanceMatrix, start_node: str,
                 stops: list[Stop] | tuple[Stop, ...]) -> list[str]:
    """Road-node path through every stop, built only from real edges."""
    path = [start_node]
    node = start_node
    for stop in stops:
        leg = matrix.leg(node, stop.node)
        if leg is None:
            raise ValueError(f"no route {node} -> {stop.node}")
        path.extend(leg.path[1:])
        node = stop.node
    return path


def route_cost(matrix: DistanceMatrix, start_node: str,
               stops: list[Stop] | tuple[Stop, ...]) -> float:
    """Total optimised-metric cost of visiting ``stops`` from start."""
    total, node = 0.0, start_node
    for stop in stops:
        total += matrix.cost(node, stop.node)
        node = stop.node
    return total if math.isfinite(total) else math.inf
