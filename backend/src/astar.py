"""A* (A-Star) search, implemented from scratch.

A* is Dijkstra with a sense of direction. Nodes are ordered by

    f(n) = g(n) + h(n)

* g(n) - exact cost of the best known path source -> n   (as in Dijkstra)
* h(n) - heuristic estimate of the remaining cost n -> goal
         (scaled Haversine distance, see ``heuristics.py``)
* f(n) - estimated cost of the cheapest route through n

Nodes pointing away from the destination get a large h(n), so they are
expanded later - or never - which is why A* usually visits fewer nodes than
Dijkstra. The heuristic is consistent, so a node's g-score is final once it
is taken from the open set, and a closed set can be used safely.

Data structures:

* ``open_set`` - binary min-heap of (f_score, h_score, node)   [heapq]
* ``g_score``  - best known cost from the source
* ``h_score``  - cached heuristic value of each discovered node
* ``f_score``  - g_score + h_score
* ``closed``   - nodes already expanded
* ``parent``   - predecessor on the best known path
"""

from __future__ import annotations

import heapq
import math
import time

from src.graph import METRIC_INDEX, Graph
from src.heuristics import haversine_km
from src.utils import SearchResult, attach_route_totals, reconstruct_path


def astar(graph: Graph, source: str, target: str, metric: str = "time",
          heuristic_scale: float = 0.0) -> SearchResult:
    """Find the cheapest route from ``source`` to ``target`` with A*.

    Args:
        graph: delivery graph.
        source, target: node IDs.
        metric: edge weight to minimise, "time" or "distance".
        heuristic_scale: metric units per straight-line km, from
            ``heuristics.calibrate_scale``. 0 turns A* into Dijkstra.
    """
    weight_index = METRIC_INDEX[metric]
    goal = graph.locations[target]
    locations = graph.locations

    start = time.perf_counter()  # ---- timed section starts ----

    def heuristic(node: str) -> float:
        """h(n): straight-line km to the goal, converted to the metric."""
        loc = locations[node]
        return heuristic_scale * haversine_km(
            loc.latitude, loc.longitude, goal.latitude, goal.longitude)

    g_score: dict[str, float] = {source: 0.0}
    h_score: dict[str, float] = {source: heuristic(source)}
    f_score: dict[str, float] = {source: h_score[source]}
    parent: dict[str, str | None] = {source: None}
    closed: set[str] = set()
    # Ties on f are broken by smaller h (the node closer to the goal).
    open_set: list[tuple[float, float, str]] = [
        (f_score[source], h_score[source], source)]
    edges_relaxed = 0

    while open_set:
        _, _, node = heapq.heappop(open_set)

        if node in closed:          # outdated heap entry
            continue
        closed.add(node)

        if node == target:          # g(target) is now optimal
            break

        for neighbour, weights in graph.adjacency[node].items():
            if neighbour in closed:
                continue
            edges_relaxed += 1
            tentative_g = g_score[node] + weights[weight_index]
            if tentative_g < g_score.get(neighbour, math.inf):
                # Better path to neighbour found: record it.
                parent[neighbour] = node
                g_score[neighbour] = tentative_g
                if neighbour not in h_score:
                    h_score[neighbour] = heuristic(neighbour)
                f_score[neighbour] = tentative_g + h_score[neighbour]
                heapq.heappush(
                    open_set,
                    (f_score[neighbour], h_score[neighbour], neighbour))

    path = reconstruct_path(parent, source, target) if target in closed else []

    execution_time = time.perf_counter() - start  # ---- timed section ends ----

    result = SearchResult(
        algorithm="A*",
        source=source,
        target=target,
        metric=metric,
        path=path,
        cost=g_score.get(target, math.inf),
        nodes_visited=len(closed),
        edges_relaxed=edges_relaxed,
        execution_time_s=execution_time,
    )
    return attach_route_totals(graph, result)
