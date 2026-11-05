"""Shared helpers: search result container, path reconstruction, totals,
benchmarking and number formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.graph import Graph


@dataclass
class SearchResult:
    """Outcome of one shortest-path search."""

    algorithm: str
    source: str
    target: str
    metric: str                      # "time" or "distance"
    path: list[str]                  # empty list -> no route exists
    cost: float                      # optimised cost (min or km)
    nodes_visited: int               # nodes expanded (popped & settled)
    edges_relaxed: int               # edges examined during the search
    execution_time_s: float          # single run, algorithm only
    total_distance_km: float = 0.0
    total_time_min: float = 0.0
    avg_time_s: float | None = None  # filled in by benchmark()
    min_time_s: float | None = None
    legs: list[tuple[str, str, float, float]] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.path)

    @property
    def edges_traversed(self) -> int:
        """Number of road segments in the final route."""
        return max(len(self.path) - 1, 0)


def reconstruct_path(parent: dict[str, str | None], source: str,
                     target: str) -> list[str]:
    """Walk the parent/predecessor links back from target to source."""
    if target not in parent:
        return []
    path = []
    node: str | None = target
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()
    return path if path[0] == source else []


def attach_route_totals(graph: Graph, result: SearchResult) -> SearchResult:
    """Fill in total distance, total time and per-leg details of the path.

    Both metrics are reported whichever one was optimised.
    """
    result.legs = []
    for u, v in zip(result.path, result.path[1:]):
        distance_km, time_min = graph.edge(u, v)
        result.legs.append((u, v, distance_km, time_min))
    result.total_distance_km = sum(leg[2] for leg in result.legs)
    result.total_time_min = sum(leg[3] for leg in result.legs)
    return result


def benchmark(search: Callable[[], SearchResult], runs: int
              ) -> tuple[float, float]:
    """Run ``search`` ``runs`` times; return (average, minimum) seconds.

    Uses each result's own ``execution_time_s`` so that only the algorithm
    body is measured, never result formatting or printing.
    """
    timings = [search().execution_time_s for _ in range(runs)]
    return sum(timings) / len(timings), min(timings)


def format_duration(seconds: float) -> str:
    """Format a duration as milliseconds plus seconds."""
    return f"{seconds * 1000:.4f} ms ({seconds:.6f} s)"


def format_ms(seconds: float) -> str:
    return f"{seconds * 1000:.4f} ms"
