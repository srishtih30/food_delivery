"""Dijkstra's shortest-path algorithm, implemented from scratch.

Idea: repeatedly settle the unvisited node with the smallest known cost from
the source. Because all weights are non-negative, once a node is popped from
the priority queue its cost can never improve, so it is final.

Data structures (named to match the textbook description):

* ``priority_queue`` - binary min-heap of (cost_so_far, node)   [heapq]
* ``distance``       - distance map: best known cost source -> node
* ``visited``        - set of processed (settled) nodes
* ``parent``         - predecessor of each node on its best known path
"""

from __future__ import annotations

import heapq
import math
import time

from src.graph import METRIC_INDEX, Graph
from src.utils import SearchResult, attach_route_totals, reconstruct_path


def dijkstra(graph: Graph, source: str, target: str,
             metric: str = "time") -> SearchResult:
    """Find the cheapest route from ``source`` to ``target``.

    Args:
        graph: delivery graph.
        source, target: node IDs (e.g. "R190", "S010").
        metric: edge weight to minimise, "time" (minutes) or "distance" (km).
    """
    weight_index = METRIC_INDEX[metric]

    start = time.perf_counter()  # ---- timed section starts ----

    distance: dict[str, float] = {source: 0.0}
    parent: dict[str, str | None] = {source: None}
    visited: set[str] = set()
    priority_queue: list[tuple[float, str]] = [(0.0, source)]
    edges_relaxed = 0

    while priority_queue:
        cost, node = heapq.heappop(priority_queue)

        # heapq has no decrease-key, so outdated entries stay in the heap;
        # skip them if the node was already settled with a lower cost.
        if node in visited:
            continue
        visited.add(node)

        # Early exit: the target's cost is final once it is popped.
        if node == target:
            break

        # Relaxation: try to improve each neighbour's cost via this node.
        for neighbour, weights in graph.adjacency[node].items():
            if neighbour in visited:
                continue
            edges_relaxed += 1
            new_cost = cost + weights[weight_index]
            if new_cost < distance.get(neighbour, math.inf):
                distance[neighbour] = new_cost
                parent[neighbour] = node
                heapq.heappush(priority_queue, (new_cost, neighbour))

    path = reconstruct_path(parent, source, target) if target in visited else []

    execution_time = time.perf_counter() - start  # ---- timed section ends ----

    result = SearchResult(
        algorithm="Dijkstra",
        source=source,
        target=target,
        metric=metric,
        path=path,
        cost=distance.get(target, math.inf),
        nodes_visited=len(visited),
        edges_relaxed=edges_relaxed,
        execution_time_s=execution_time,
    )
    return attach_route_totals(graph, result)
