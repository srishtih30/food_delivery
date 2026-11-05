"""Simulated delivery drivers (dispatch simulation, stage 1).

A driver is placed on an existing, connected graph node (society or
restaurant) - never a new/synthetic location - and moves across the same
graph the routing algorithms already operate on.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.graph import Graph


class DriverStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    TO_PICKUP = "TO_PICKUP"
    TO_DROPOFF = "TO_DROPOFF"
    AVAILABLE_AFTER_DELIVERY = "AVAILABLE_AFTER_DELIVERY"


@dataclass
class Driver:
    """A simulated driver's current state."""

    driver_id: str
    current_node: str
    status: DriverStatus = DriverStatus.AVAILABLE
    assigned_order: str | None = None
    total_distance: float = 0.0
    total_travel_time: float = 0.0
    completed_orders: int = 0
    # Multi-stop routing (pdp.py). Capacity = how many active (assigned,
    # not yet delivered) orders the driver may hold; each order is 1 unit.
    # The legacy single-order DispatchEngine ignores these fields.
    capacity: int = 1
    active_orders: list[str] = field(default_factory=list)
    onboard: set[str] = field(default_factory=set)   # picked up, not dropped
    route: list[Any] = field(default_factory=list)   # pdp.Stop, in order
    clock: float = 0.0          # time of the last completed stop (minutes)
    stop_log: list[tuple[float, Any]] = field(default_factory=list)
    road_path: list[str] = field(default_factory=list)  # nodes driven
    road_log: list[tuple[float, str, str]] = field(default_factory=list)
    # ^ (time the road was entered, from node, to node) for every road driven
    visit_log: list[dict[str, Any]] = field(default_factory=list)
    # ^ per completed stop: arrival, departure, cumulative km / minutes

    @property
    def remaining_capacity(self) -> int:
        return self.capacity - len(self.active_orders)


def generate_drivers(graph: Graph, num_drivers: int, seed: int,
                     capacity: int = 1) -> list[Driver]:
    """Create ``num_drivers`` drivers, reproducibly, on real graph nodes.

    Drivers are distributed (with replacement) across every node that has at
    least one outgoing road, so every driver can actually move.
    """
    if num_drivers <= 0:
        raise ValueError("num_drivers must be positive.")
    if capacity <= 0:
        raise ValueError("capacity must be positive.")

    rng = random.Random(seed)
    connected_nodes = sorted(nid for nid in graph.locations
                             if graph.is_connected_node(nid))
    if not connected_nodes:
        raise ValueError("Graph has no connected nodes to place drivers on.")

    return [
        Driver(driver_id=f"D{i:03d}", current_node=rng.choice(connected_nodes),
               capacity=capacity)
        for i in range(1, num_drivers + 1)
    ]
