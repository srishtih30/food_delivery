"""Stop-to-stop shortest-path costs for multi-stop routing.

The pickup-and-delivery planner (``pdp.py``) only reasons about *stops*
(restaurants and societies). It needs the shortest road route between any
two stops many times, so this module caches them:

    matrix.leg("R190", "S010") -> Leg(distance_km, time_min, cost, path)

Two back-ends give the same optimal costs:

* ``"tree"`` (default): one shortest-path tree per source node, built on
  first use with the same Dijkstra relaxation as ``dijkstra.py`` but
  without the early exit, so one run answers every target. This keeps
  dispatch latency low.
* ``"dijkstra"`` / ``"astar"``: the project's existing point-to-point
  searches, called once per (source, target) pair and cached. The
  Dijkstra vs A* choice is still available in the multi-stop simulation.

``tests/test_multistop.py`` checks that the tree back-end returns exactly the
same costs as the existing ``dijkstra()`` and ``astar()``.

Every ``Leg.path`` is a sequence of real graph nodes joined by existing
directed edges; no direct edge between stops is ever invented. Unreachable
pairs return ``None``.

Road closures: removing a road can only make routes that *used* it longer;
every other cached shortest path stays shortest. :meth:`road_closed`
therefore drops just the cached legs whose path uses the closed road (an
index from road to legs is kept for this). Reopening a road can shorten any
route, so :meth:`road_reopened` clears everything.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from src.astar import astar
from src.cancellation import checkpoint
from src.dijkstra import dijkstra
from src.graph import METRIC_INDEX, Graph
from src.heuristics import calibrate_scale


@dataclass(frozen=True)
class Leg:
    """Shortest road route between two stops."""

    distance_km: float
    time_min: float
    cost: float                 # the optimised metric (km or min)
    path: tuple[str, ...]       # real graph nodes, source ... target


class DistanceMatrix:
    """Lazily computed, cached shortest paths between graph nodes."""

    ALGORITHMS = ("tree", "dijkstra", "astar")

    def __init__(self, graph: Graph, metric: str = "time",
                 algorithm: str = "tree") -> None:
        if metric not in METRIC_INDEX:
            raise ValueError(f"Unknown metric '{metric}'.")
        if algorithm not in self.ALGORITHMS:
            raise ValueError(f"Unknown algorithm '{algorithm}'. Choose from "
                             f"{self.ALGORITHMS}.")
        self.graph = graph
        self.metric = metric
        self.algorithm = algorithm
        self._index = METRIC_INDEX[metric]
        self._heuristic_scale = (calibrate_scale(graph, metric)
                                 if algorithm == "astar" else 0.0)
        self._legs: dict[tuple[str, str], Leg | None] = {}
        self._trees: set[str] = set()
        # road (u, v) -> cached leg keys whose path uses it
        self._road_users: dict[tuple[str, str], set[tuple[str, str]]] = {}
        self.searches = 0       # shortest-path runs performed (diagnostics)

    def invalidate(self) -> None:
        """Forget every cached route (call after the graph changes)."""
        self._legs.clear()
        self._trees.clear()
        self._road_users.clear()

    def road_closed(self, source: str, target: str) -> int:
        """Drop cached legs that use the (now closed) road; returns how
        many were dropped. Call after ``graph.close_road``."""
        users = self._road_users.pop((source, target), set())
        for key in users:
            if self._legs.pop(key, None) is not None:
                # Its shortest-path tree must be rebuilt on next use.
                self._trees.discard(key[0])
        return len(users)

    def road_reopened(self) -> None:
        """Call after ``graph.reopen_road``."""
        self.invalidate()

    def _store(self, key: tuple[str, str], leg: Leg | None) -> None:
        self._legs[key] = leg
        if leg is not None:
            for road in zip(leg.path, leg.path[1:]):
                self._road_users.setdefault(road, set()).add(key)

    def leg(self, source: str, target: str) -> Leg | None:
        """Shortest route source -> target, or ``None`` if unreachable."""
        key = (source, target)
        if key in self._legs:
            return self._legs[key]
        if source == target:
            leg = Leg(0.0, 0.0, 0.0, (source,)) \
                if source in self.graph.locations else None
            self._legs[key] = leg
            return leg
        if source not in self.graph.locations \
                or target not in self.graph.locations:
            self._legs[key] = None
            return None
        if self.algorithm == "tree":
            if source not in self._trees:
                self._build_tree(source)
            # Nodes the tree never reached are unreachable.
            return self._legs.setdefault(key, None)
        self._store(key, self._point_to_point(source, target))
        return self._legs[key]

    def cost(self, source: str, target: str) -> float:
        leg = self.leg(source, target)
        return math.inf if leg is None else leg.cost

    def precompute(self, nodes: list[str] | None = None) -> None:
        """Fill the cache for every source in ``nodes`` (default: all)."""
        for node in sorted(nodes if nodes is not None
                           else self.graph.locations):
            if self.algorithm == "tree":
                if node not in self._trees:
                    self._build_tree(node)
            else:
                for target in sorted(self.graph.locations):
                    self.leg(node, target)

    # ------------------------------------------------------------------

    def _point_to_point(self, source: str, target: str) -> Leg | None:
        checkpoint()
        self.searches += 1
        if self.algorithm == "astar":
            result = astar(self.graph, source, target, self.metric,
                           self._heuristic_scale)
        else:
            result = dijkstra(self.graph, source, target, self.metric)
        if not result.found:
            return None
        return Leg(result.total_distance_km, result.total_time_min,
                   result.cost, tuple(result.path))

    def _build_tree(self, source: str) -> None:
        """Dijkstra from ``source`` to every node (no early exit)."""
        checkpoint()
        self.searches += 1
        adjacency, index = self.graph.adjacency, self._index
        cost = {source: 0.0}
        km = {source: 0.0}
        minutes = {source: 0.0}
        parent: dict[str, str | None] = {source: None}
        visited: set[str] = set()
        heap = [(0.0, source)]
        while heap:
            c, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            for neighbour, weights in adjacency[node].items():
                if neighbour in visited:
                    continue
                new_cost = c + weights[index]
                if new_cost < cost.get(neighbour, math.inf):
                    cost[neighbour] = new_cost
                    km[neighbour] = km[node] + weights[0]
                    minutes[neighbour] = minutes[node] + weights[1]
                    parent[neighbour] = node
                    heapq.heappush(heap, (new_cost, neighbour))

        for target in visited:
            path = []
            node: str | None = target
            while node is not None:
                path.append(node)
                node = parent[node]
            path.reverse()
            key = (source, target)
            if key in self._legs and self._legs[key] is not None:
                continue     # still valid (unaffected by any closure)
            self._store(key, Leg(km[target], minutes[target], cost[target],
                                 tuple(path)))
        # Targets the tree did not reach are unreachable from source.
        for target in self.graph.locations:
            self._legs.setdefault((source, target), None)
        self._trees.add(source)
