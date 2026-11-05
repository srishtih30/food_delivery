"""Greedy dispatch baseline (dispatch simulation, stage 3).

For each pending order this picks the available driver with the cheapest
route to the restaurant, then simulates that driver's full trip:

    driver -> restaurant (pickup) -> society (drop-off)

It is a thin layer on top of the existing Dijkstra/A* search - no new
shortest-path algorithm is implemented here, and the algorithm to use
(``"dijkstra"`` or ``"astar"``) is configurable.
"""

from __future__ import annotations

from src.astar import astar
from src.dijkstra import dijkstra
from src.driver import Driver, DriverStatus
from src.graph import Graph
from src.order import Order, OrderStatus
from src.serviceability import ServiceabilityPolicy
from src.utils import SearchResult

ALGORITHMS = {"dijkstra": dijkstra, "astar": astar}


class DispatchEngine:
    """Assigns orders to drivers using nearest-available-driver logic."""

    def __init__(self, graph: Graph, algorithm: str = "astar",
                metric: str = "time", heuristic_scale: float = 0.0,
                serviceability: ServiceabilityPolicy | None = None) -> None:
        if algorithm not in ALGORITHMS:
            raise ValueError(
                f"Unknown algorithm '{algorithm}'. Choose from "
                f"{sorted(ALGORITHMS)}.")
        self.graph = graph
        self.algorithm = algorithm
        self.metric = metric
        self.heuristic_scale = heuristic_scale
        self.serviceability = serviceability

    def _search(self, source: str, target: str) -> SearchResult:
        if self.algorithm == "astar":
            return astar(self.graph, source, target, self.metric,
                        self.heuristic_scale)
        return dijkstra(self.graph, source, target, self.metric)

    def validate_order(self, order: Order) -> bool:
        """Serviceability gate, applied before any driver is considered.

        An order the policy rejects moves PENDING -> REJECTED with its
        reason recorded, so it can never be assigned to a driver. Without a
        policy every order passes (previous behaviour).
        """
        if self.serviceability is None:
            return True
        result = self.serviceability.check(order.restaurant_id,
                                           order.society_id)
        if not result.serviceable:
            order.transition(OrderStatus.REJECTED)
            order.rejection_reason = result.reason.value
            order.rejection_message = result.message
        return result.serviceable

    def find_best_driver(self, drivers: list[Driver], restaurant_id: str
                         ) -> tuple[Driver, SearchResult] | None:
        """Return the available driver with the cheapest route to the
        restaurant (by ``self.metric``: travel time, or distance if the
        metric is "distance"), together with that route. ``None`` if no
        available driver can reach the restaurant at all."""
        best: tuple[Driver, SearchResult] | None = None
        for driver in drivers:
            if driver.status is not DriverStatus.AVAILABLE:
                continue
            result = self._search(driver.current_node, restaurant_id)
            if not result.found:
                continue
            if best is None or result.cost < best[1].cost:
                best = (driver, result)
        return best

    def dispatch_order(self, order: Order, drivers: list[Driver]) -> bool:
        """Assign the best available driver to ``order`` and simulate the
        pickup -> delivery trip end to end.

        Returns True if the order was delivered. Returns False if the order
        fails the serviceability check (it becomes REJECTED) or no available
        driver could reach the restaurant (it stays PENDING).

        On success the driver ends in ``AVAILABLE_AFTER_DELIVERY``; call
        :meth:`release_completed_drivers` to make it assignable again.
        """
        if order.status is not OrderStatus.PENDING:
            raise ValueError(f"Order {order.order_id} is not PENDING.")
        if not self.validate_order(order):
            return False

        match = self.find_best_driver(drivers, order.restaurant_id)
        if match is None:
            return False
        driver, pickup_result = match

        order.transition(OrderStatus.ASSIGNED)
        order.assigned_driver = driver.driver_id
        driver.status = DriverStatus.TO_PICKUP
        driver.assigned_order = order.order_id

        # Leg 1: driver -> restaurant (pickup).
        order.pickup_route = pickup_result.path
        driver.current_node = order.restaurant_id
        order.pickup_time = pickup_result.total_time_min
        order.transition(OrderStatus.PICKED_UP)
        driver.status = DriverStatus.TO_DROPOFF

        # Leg 2: restaurant -> society (drop-off). Guaranteed reachable,
        # since generate_orders() only keeps routable (restaurant, society)
        # pairs.
        delivery_result = self._search(order.restaurant_id, order.society_id)
        if not delivery_result.found:
            raise RuntimeError(
                f"No route from {order.restaurant_id} to {order.society_id} "
                f"for order {order.order_id}.")
        order.delivery_route = delivery_result.path
        driver.current_node = order.society_id
        order.delivery_time = order.pickup_time + delivery_result.total_time_min
        order.transition(OrderStatus.DELIVERED)

        order.total_distance = (pickup_result.total_distance_km
                                + delivery_result.total_distance_km)
        order.total_travel_time = (pickup_result.total_time_min
                                   + delivery_result.total_time_min)

        driver.total_distance += order.total_distance
        driver.total_travel_time += order.total_travel_time
        driver.completed_orders += 1
        driver.assigned_order = None
        driver.status = DriverStatus.AVAILABLE_AFTER_DELIVERY
        return True

    @staticmethod
    def release_completed_drivers(drivers: list[Driver]) -> None:
        """Move drivers that just finished a delivery back to AVAILABLE."""
        for driver in drivers:
            if driver.status is DriverStatus.AVAILABLE_AFTER_DELIVERY:
                driver.status = DriverStatus.AVAILABLE
