"""Graph construction (pipeline stage 3).

Nodes are every society (S001...) and every restaurant (R001...). Edges are
*directed*, because the dataset records different forward and return values
for society <-> restaurant roads (one-way streets / different turns):

* society_connections : S_a <-> S_b, same distance/time both ways
* society_restaurants : S -> R uses (Distance_km, Travel_Time_min)
                        R -> S uses (Return_Distance_km, Return_Travel_Time_min)

Adjacency representation (dict of dicts)::

    graph.adjacency = {
        "R190": {"S001": (0.549, 7.0)},            # neighbour: (km, min)
        "S001": {"S003": (2.691, 9.3), "R190": (0.549, 6.8), ...},
        ...
    }
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from src.data_loader import DataError, Dataset

# Position of each metric inside an edge's (distance_km, time_min) tuple.
METRIC_INDEX = {"distance": 0, "time": 1}
METRIC_UNITS = {"distance": "km", "time": "min"}


@dataclass(frozen=True)
class Location:
    """A node in the delivery graph (a society or a restaurant)."""

    node_id: str
    name: str
    kind: str          # "Society" or "Restaurant"
    area: str
    address: str
    latitude: float
    longitude: float
    cuisine: str = ""
    phone: str = ""
    rating: float | None = None


class Graph:
    """Directed, weighted graph with two weights per edge: km and minutes."""

    def __init__(self) -> None:
        self.locations: dict[str, Location] = {}
        self.adjacency: dict[str, dict[str, tuple[float, float]]] = {}
        # Temporarily blocked roads: (source, target) -> original weights.
        self.closed_roads: dict[tuple[str, str], tuple[float, float]] = {}
        # Neighbour order before the first closure of a node, so reopening
        # restores the exact adjacency order (and hence tie-breaking).
        self._neighbour_order: dict[str, list[str]] = {}

    def add_location(self, location: Location) -> None:
        self.locations[location.node_id] = location
        self.adjacency.setdefault(location.node_id, {})

    def add_edge(self, source: str, target: str,
                 distance_km: float, time_min: float) -> None:
        """Add a directed edge; for duplicates keep the faster road."""
        existing = self.adjacency[source].get(target)
        if existing is None or (time_min, distance_km) < existing[::-1]:
            self.adjacency[source][target] = (distance_km, time_min)

    def edge(self, source: str, target: str) -> tuple[float, float]:
        """Return ``(distance_km, time_min)`` of the edge source -> target."""
        return self.adjacency[source][target]

    def has_edge(self, source: str, target: str) -> bool:
        return target in self.adjacency.get(source, {})

    def close_road(self, source: str, target: str) -> tuple[float, float]:
        """Block the existing directed road source -> target.

        The search algorithms read ``adjacency``, so a closed road is simply
        absent from it. Only a road that exists (and is open) can be closed.
        Returns the road's (distance_km, time_min).
        """
        if not self.has_edge(source, target):
            if (source, target) in self.closed_roads:
                raise ValueError(f"Road {source} -> {target} is already "
                                 f"closed.")
            raise ValueError(f"No road {source} -> {target} in the graph.")
        self._neighbour_order.setdefault(source,
                                         list(self.adjacency[source]))
        weights = self.adjacency[source].pop(target)
        self.closed_roads[(source, target)] = weights
        return weights

    def reopen_road(self, source: str, target: str) -> None:
        """Restore a road closed with :meth:`close_road`, with its original
        weights and neighbour order."""
        if (source, target) not in self.closed_roads:
            raise ValueError(f"Road {source} -> {target} is not closed.")
        weights = self.closed_roads.pop((source, target))
        current = self.adjacency[source]
        current[target] = weights
        order = self._neighbour_order.get(source, [])
        self.adjacency[source] = {n: current[n] for n in order
                                  if n in current}
        self.adjacency[source].update(current)   # anything added later
        if not any(s == source for s, _ in self.closed_roads):
            self._neighbour_order.pop(source, None)

    def reopen_all_roads(self) -> None:
        for source, target in list(self.closed_roads):
            self.reopen_road(source, target)

    def has_node(self, node_id: str) -> bool:
        return node_id in self.locations

    def is_connected_node(self, node_id: str) -> bool:
        """True if the node has at least one outgoing road."""
        return bool(self.adjacency.get(node_id))

    @property
    def node_count(self) -> int:
        return len(self.locations)

    @property
    def edge_count(self) -> int:
        return sum(len(nbrs) for nbrs in self.adjacency.values())


def _valid_weights(distance_km: float, time_min: float) -> bool:
    """Weights must be real, finite and non-negative (Dijkstra requirement)."""
    return all(
        not pd.isna(w) and math.isfinite(w) and w >= 0
        for w in (distance_km, time_min)
    )


def _location_from_row(row: pd.Series, id_col: str, name_col: str,
                       kind: str) -> Location:
    rating = row.get("Rating")
    return Location(
        node_id=row[id_col],
        name=row[name_col],
        kind=kind,
        area=row["Area"],
        address=row["Address"],
        latitude=float(row["Latitude"]),
        longitude=float(row["Longitude"]),
        cuisine=row["Cuisine"],
        phone=row["Phone"],
        rating=None if pd.isna(rating) else float(rating),
    )


def build_graph(dataset: Dataset) -> Graph:
    """Convert the cleaned dataset into a :class:`Graph`.

    Edges referring to unknown nodes, self-loops and edges with invalid
    weights are skipped and reported in ``dataset.warnings``.
    """
    graph = Graph()
    for _, row in dataset.societies.iterrows():
        graph.add_location(
            _location_from_row(row, "Society_ID", "Society_Name", "Society"))
    for _, row in dataset.restaurants.iterrows():
        graph.add_location(_location_from_row(
            row, "Restaurant_ID", "Restaurant_Name", "Restaurant"))

    # (source, target, distance_km, time_min) for every directed edge.
    candidate_edges: list[tuple[str, str, float, float]] = []

    # Society <-> society roads are two-way with identical weights.
    for row in dataset.connections.itertuples(index=False):
        candidate_edges.append(
            (row.Society_A, row.Society_B, row.Distance_km,
             row.Travel_Time_min))
        candidate_edges.append(
            (row.Society_B, row.Society_A, row.Distance_km,
             row.Travel_Time_min))

    # Society <-> restaurant roads have different forward/return weights.
    for row in dataset.society_restaurants.itertuples(index=False):
        candidate_edges.append(
            (row.Society_ID, row.Restaurant_ID, row.Distance_km,
             row.Travel_Time_min))
        candidate_edges.append(
            (row.Restaurant_ID, row.Society_ID, row.Return_Distance_km,
             row.Return_Travel_Time_min))

    unknown = invalid = 0
    for source, target, distance_km, time_min in candidate_edges:
        if not (graph.has_node(source) and graph.has_node(target)):
            unknown += 1
        elif source == target or not _valid_weights(distance_km, time_min):
            invalid += 1
        else:
            graph.add_edge(source, target, float(distance_km),
                           float(time_min))

    if unknown:
        dataset.warnings.append(
            f"{unknown} edge(s) skipped: they reference unknown locations.")
    if invalid:
        dataset.warnings.append(
            f"{invalid} edge(s) skipped: self-loop or invalid distance/time.")
    if graph.edge_count == 0:
        raise DataError("The dataset produced a graph with no valid roads.")
    return graph
