"""Simulated delivery orders (dispatch simulation, stage 2).

Every order links a real restaurant node to a real society node from the
existing dataset. Without a serviceability policy, an order is only created
for a pair that has a restaurant -> society route (checked with the existing
Dijkstra search). With a policy, unserviceable pairs are either skipped
(default) or kept as explicit REJECTED orders with their reason
(``reject_unserviceable=True``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from typing import TYPE_CHECKING

from src.dijkstra import dijkstra
from src.graph import Graph

if TYPE_CHECKING:
    from src.serviceability import ServiceabilityPolicy


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"   # failed the serviceability check; never assigned
    FAILED = "FAILED"       # serviceable, but no driver could take it in time


# The only legal forward transitions. This is what guarantees an order can
# never reach DELIVERED without having passed through PICKED_UP.
_VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.ASSIGNED, OrderStatus.REJECTED,
                          OrderStatus.FAILED},
    # ASSIGNED -> FAILED / PICKED_UP -> FAILED only when a road closure
    # makes the order's stops unreachable (pdp.PickupDeliveryPlanner).
    OrderStatus.ASSIGNED: {OrderStatus.PICKED_UP, OrderStatus.FAILED},
    OrderStatus.PICKED_UP: {OrderStatus.DELIVERED, OrderStatus.FAILED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.FAILED: set(),
}


@dataclass
class Order:
    """A simulated order's current state."""

    order_id: str
    restaurant_id: str
    society_id: str
    status: OrderStatus = OrderStatus.PENDING
    assigned_driver: str | None = None
    pickup_time: float | None = None      # minutes, relative to assignment
    delivery_time: float | None = None    # minutes, relative to assignment
    pickup_route: list[str] = field(default_factory=list)    # driver -> restaurant
    delivery_route: list[str] = field(default_factory=list)  # restaurant -> society
    total_distance: float = 0.0
    total_travel_time: float = 0.0
    rejection_reason: str | None = None   # serviceability Reason value
    rejection_message: str = ""
    # Multi-stop simulation clock, in minutes since the simulation start.
    # ``None`` means "not modelled": ready immediately / no deadline.
    created_at: float = 0.0
    ready_at: float | None = None        # food ready for pickup
    deadline: float | None = None        # latest allowed drop-off time
    assigned_at: float | None = None
    picked_up_at: float | None = None
    delivered_at: float | None = None
    failure_reason: str | None = None    # set when status is FAILED
    risk_reason: str | None = None       # e.g. deadline at risk after closure
    ride_limit_min: float | None = None  # max pickup -> drop-off, fixed at assignment
    reassignments: int = 0               # safe releases back to PENDING

    def release(self) -> None:
        """ASSIGNED -> PENDING. Only for an order that has not been picked
        up and whose driver can no longer reach it (road closure); the
        dispatcher may then offer it to other drivers."""
        if self.status is not OrderStatus.ASSIGNED:
            raise ValueError(f"Cannot release {self.order_id} in status "
                             f"{self.status.value}")
        self.status = OrderStatus.PENDING
        self.assigned_driver = None
        self.assigned_at = None
        self.ride_limit_min = None      # recomputed on the next assignment
        self.reassignments += 1

    def transition(self, new_status: OrderStatus) -> None:
        """Advance the order's status, enforcing the PENDING -> ASSIGNED ->
        PICKED_UP -> DELIVERED order. Raises ValueError on any skip
        (e.g. PENDING -> DELIVERED or ASSIGNED -> DELIVERED)."""
        allowed = _VALID_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise ValueError(
                f"Illegal order transition for {self.order_id}: "
                f"{self.status.value} -> {new_status.value}")
        self.status = new_status


def generate_orders(graph: Graph, num_orders: int, seed: int,
                    metric: str = "time", max_attempts: int = 20000,
                    serviceability: ServiceabilityPolicy | None = None,
                    reject_unserviceable: bool = False,
                    ) -> list[Order]:
    """Create ``num_orders`` orders, reproducibly, on real restaurant/society
    nodes that already have a valid route between them.

    With a ``serviceability`` policy, a route existing is not enough:

    * ``reject_unserviceable=False`` - only pairs the policy accepts are
      generated.
    * ``reject_unserviceable=True``  - every unique pair is generated; pairs
      the policy refuses become REJECTED orders carrying the reason, so the
      refused demand is visible instead of silently dropped.

    Raises:
        ValueError: if ``num_orders`` valid, unique (restaurant, society)
            pairs cannot be found within ``max_attempts`` tries.
    """
    if num_orders <= 0:
        raise ValueError("num_orders must be positive.")

    rng = random.Random(seed)
    restaurants = sorted(nid for nid, loc in graph.locations.items()
                         if loc.kind == "Restaurant" and graph.is_connected_node(nid))
    societies = sorted(nid for nid, loc in graph.locations.items()
                       if loc.kind == "Society" and graph.is_connected_node(nid))
    if not restaurants or not societies:
        raise ValueError("Graph has no connected restaurant/society nodes.")

    orders: list[Order] = []
    seen_pairs: set[tuple[str, str]] = set()
    attempts = 0
    while len(orders) < num_orders and attempts < max_attempts:
        attempts += 1
        restaurant_id = rng.choice(restaurants)
        society_id = rng.choice(societies)
        pair = (restaurant_id, society_id)
        if pair in seen_pairs:
            continue
        rejection = None
        if serviceability is not None:
            check = serviceability.check(restaurant_id, society_id)
            if not check.serviceable:
                if not reject_unserviceable:
                    continue
                rejection = check
        elif not dijkstra(graph, restaurant_id, society_id, metric).found:
            continue
        seen_pairs.add(pair)
        order = Order(
            order_id=f"O{len(orders) + 1:04d}",
            restaurant_id=restaurant_id,
            society_id=society_id,
        )
        if rejection is not None:
            order.transition(OrderStatus.REJECTED)
            order.rejection_reason = rejection.reason.value
            order.rejection_message = rejection.message
        orders.append(order)

    if len(orders) < num_orders:
        raise ValueError(
            f"Could only generate {len(orders)}/{num_orders} orders with a "
            f"valid (and serviceable, if a policy is set) restaurant -> "
            f"society route after {max_attempts} "
            f"attempts.")
    return orders
