"""Step-by-step traces of the *existing* Dijkstra and A* implementations.

This module provides "Dijkstra + Trace" and "A* + Trace" for the
visualization WITHOUT modifying or duplicating ``dijkstra.py``/``astar.py``.
The unmodified functions are simply run while two things are observed:

1. Priority-queue operations. Inside the algorithm module, the global name
   ``heapq`` is temporarily replaced (``unittest.mock.patch``) by a recorder
   that forwards every ``heappush``/``heappop`` to the real ``heapq`` and logs
   the entry. Dijkstra pushes ``(cost, node)``; A* pushes
   ``(f, h, node)``, so g = f - h. These are exactly the values the
   algorithm uses, including A*'s real heuristic.
2. Node expansions. The algorithm receives a shallow copy of the graph whose
   ``adjacency`` dict logs each ``adjacency[node]`` lookup, which is the
   moment the algorithm starts relaxing that node's outgoing roads.

The raw event log is then turned into presentation "steps" (one per heap
pop). The traced run is checked against a normal run (same path, same cost,
same node and edge counts) and the result is reported as ``verified``.

Execution times shown in the visualization come from normal (untraced) runs,
because recording adds overhead.
"""

from __future__ import annotations

import copy
import heapq as _real_heapq
import threading
from types import ModuleType
from typing import Any, Callable
from unittest import mock

from src import astar as astar_module
from src import dijkstra as dijkstra_module
from src.graph import Graph
from src.utils import SearchResult

# Patching a module global is process-wide; serialise traced runs.
_TRACE_LOCK = threading.Lock()

FRONTIER_PREVIEW = 8  # priority-queue rows shown per step


class _HeapRecorder:
    """Drop-in replacement for the ``heapq`` module that logs operations.

    The patch on the algorithm module is process-wide, so other threads
    (planner runs, serviceability checks) may call through it while a trace
    is being recorded. Only the thread that owns the trace is recorded;
    every other caller is forwarded straight to the real ``heapq``. This
    lets the API serve other requests concurrently with a traced search.
    """

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self._events = events
        self._owner = threading.get_ident()

    def heappush(self, heap: list, item: tuple) -> None:
        if threading.get_ident() == self._owner:
            self._events.append(("push", item))
        _real_heapq.heappush(heap, item)

    def heappop(self, heap: list) -> tuple:
        item = _real_heapq.heappop(heap)
        if threading.get_ident() == self._owner:
            self._events.append(("pop", item))
        return item

    def __getattr__(self, name: str) -> Any:
        # heapify, heapreplace, ... (not recorded) for any other caller.
        return getattr(_real_heapq, name)


class _AdjacencyRecorder(dict):
    """Adjacency dict that logs every ``adjacency[node]`` lookup."""

    def __init__(self, adjacency: dict, events: list[tuple[str, Any]]) -> None:
        super().__init__(adjacency)
        self._events = events

    def __getitem__(self, node: str) -> dict:
        self._events.append(("expand", node))
        return super().__getitem__(node)


def _run_recorded(module: ModuleType, search: Callable[..., SearchResult],
                  graph: Graph, *args: Any
                  ) -> tuple[SearchResult, list[tuple[str, Any]]]:
    """Run an unmodified search function and capture its event log."""
    events: list[tuple[str, Any]] = []
    traced_graph = copy.copy(graph)  # shares nodes/edges with the original
    traced_graph.adjacency = _AdjacencyRecorder(graph.adjacency, events)
    with _TRACE_LOCK, mock.patch.object(module, "heapq",
                                        _HeapRecorder(events)):
        result = search(traced_graph, *args)
    return result, events


def _decode(item: tuple, is_astar: bool) -> dict[str, float]:
    """Heap entry -> {"g", "h", "f"} (Dijkstra: h = 0, f = g)."""
    if is_astar:
        f_value, h_value, _ = item
        return {"g": f_value - h_value, "h": h_value, "f": f_value}
    return {"g": item[0], "h": 0.0, "f": item[0]}


def _rounded(values: dict[str, float]) -> dict[str, float]:
    return {key: round(val, 4) for key, val in values.items()}


def _build_steps(graph: Graph, events: list[tuple[str, Any]], target: str,
                 is_astar: bool) -> list[dict[str, Any]]:
    """Convert the raw event log into one step per priority-queue pop."""
    steps: list[dict[str, Any]] = []
    visited: set[str] = set()
    frontier: dict[str, dict[str, float]] = {}  # best open entry per node
    open_step: dict[str, Any] | None = None     # step collecting pushes

    def order_key(node: str) -> tuple:
        entry = frontier[node]
        # Same ordering the heap uses: (cost, node) or (f, h, node).
        return ((entry["f"], entry["h"], node) if is_astar
                else (entry["g"], node))

    def close_step() -> None:
        """Snapshot the frontier once a step's pushes are complete."""
        if open_step is None:
            return
        ranked = sorted(frontier, key=order_key)
        open_step["frontier_size"] = len(frontier)
        open_step["frontier_top"] = [
            {"node": n, **_rounded(frontier[n])}
            for n in ranked[:FRONTIER_PREVIEW]]

    for kind, payload in events:
        if kind == "pop":
            close_step()
            node = payload[-1]
            values = _rounded(_decode(payload, is_astar))
            if node in visited:
                # Lazy deletion: an outdated duplicate entry is discarded.
                open_step = {"kind": "stale", "node": node, **values,
                             "explored": len(visited), "examined": [],
                             "relaxed": []}
            else:
                visited.add(node)
                frontier.pop(node, None)
                open_step = {"kind": "goal" if node == target else "expand",
                             "node": node, **values,
                             "explored": len(visited), "examined": [],
                             "relaxed": [], "_expanded": False}
            steps.append(open_step)

        elif kind == "push":
            node = payload[-1]
            frontier[node] = _decode(payload, is_astar)
            if open_step is not None:
                open_step["relaxed"].append(
                    {"node": node, **_rounded(frontier[node])})

        elif kind == "expand":
            # Only a lookup right after a fresh pop is an expansion; later
            # lookups come from route-total calculation after the search.
            if (open_step is not None and open_step.get("_expanded") is False
                    and open_step["node"] == payload):
                open_step["_expanded"] = True
                # The algorithm skips neighbours that are already visited.
                open_step["examined"] = [
                    n for n in graph.adjacency[payload] if n not in visited]

    close_step()
    for step in steps:
        step.pop("_expanded", None)
    return steps


def _result_summary(result: SearchResult) -> dict[str, Any]:
    return {
        "algorithm": result.algorithm,
        "found": result.found,
        "path": result.path,
        "cost": round(result.cost, 4) if result.found else None,
        "total_distance_km": round(result.total_distance_km, 4),
        "total_time_min": round(result.total_time_min, 4),
        "nodes_visited": result.nodes_visited,
        "edges_relaxed": result.edges_relaxed,
        "edges_traversed": result.edges_traversed,
        "execution_time_ms": result.execution_time_s * 1000,
        "legs": [{"from": u, "to": v, "km": km, "min": minutes}
                 for u, v, km, minutes in result.legs],
    }


def _trace(module: ModuleType, search: Callable[..., SearchResult],
           graph: Graph, source: str, target: str, is_astar: bool,
           *extra: Any) -> dict[str, Any]:
    normal = search(graph, source, target, *extra)
    traced, events = _run_recorded(module, search, graph, source, target,
                                   *extra)
    steps = _build_steps(graph, events, target, is_astar)

    fresh_steps = [s for s in steps if s["kind"] != "stale"]
    verified = (
        traced.path == normal.path
        and traced.found == normal.found
        and (not normal.found or abs(traced.cost - normal.cost) < 1e-9)
        and traced.nodes_visited == normal.nodes_visited == len(fresh_steps)
        and traced.edges_relaxed == normal.edges_relaxed
        == sum(len(s["examined"]) for s in steps)
    )
    summary = _result_summary(normal)
    summary.update(steps=steps, verified=verified)
    return summary


def dijkstra_with_trace(graph: Graph, source: str, target: str,
                        metric: str = "time") -> dict[str, Any]:
    """Run the existing ``dijkstra`` and return its result plus steps."""
    return _trace(dijkstra_module, dijkstra_module.dijkstra, graph,
                  source, target, False, metric)


def astar_with_trace(graph: Graph, source: str, target: str,
                     metric: str = "time",
                     heuristic_scale: float = 0.0) -> dict[str, Any]:
    """Run the existing ``astar`` and return its result plus steps."""
    return _trace(astar_module, astar_module.astar, graph,
                  source, target, True, metric, heuristic_scale)
