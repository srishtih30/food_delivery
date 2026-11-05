"""A* heuristic: scaled Haversine (great-circle) distance to the goal.

Every location in the dataset has latitude/longitude, so the natural
heuristic is the straight-line distance "as the crow flies":

    h(n) = scale * haversine_km(n, goal)

Why this is appropriate
-----------------------
A* returns an optimal route only if h(n) never over-estimates the true
remaining cost (*admissible*); if it is also *consistent*
(h(u) <= w(u, v) + h(v) for every edge) each node needs to be expanded only
once, so A* can use a closed set exactly like Dijkstra's visited set.

``scale`` converts kilometres of straight line into the optimised metric
and is calibrated from the dataset itself:

    scale = min over all edges (u, v) of  weight(u, v) / haversine_km(u, v)

* metric = "distance": scale is ~1 (a road is never shorter than the straight
  line), but calibrating guards against rounding / coordinate noise.
* metric = "time": scale = 1 / (fastest straight-line speed on any road),
  i.e. minutes per straight-line km for the quickest road in the data.
  No route can cover the remaining straight-line distance faster than that.

Consistency proof sketch: for any edge, weight(u, v) >= scale * hav(u, v),
and the Haversine distance obeys the triangle inequality, so
h(u) = scale*hav(u, g) <= scale*hav(u, v) + scale*hav(v, g) <= w(u, v) + h(v).
Consistency implies admissibility, so A* finds the same optimal cost as
Dijkstra while usually expanding fewer nodes.
"""

from __future__ import annotations

import math

from src.graph import METRIC_INDEX, Graph

EARTH_RADIUS_KM = 6371.0088  # mean Earth radius (IUGG)

# Edges shorter than this (straight line) give no useful calibration signal.
_MIN_CALIBRATION_KM = 1e-6


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def straight_line_km(graph: Graph, node_a: str, node_b: str) -> float:
    """Haversine distance between two graph nodes."""
    a, b = graph.locations[node_a], graph.locations[node_b]
    return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)


def calibrate_scale(graph: Graph, metric: str) -> float:
    """Largest ``scale`` for which scale * haversine is still consistent.

    Computed once per graph and metric (like graph construction), so it is
    not part of the per-query A* execution time.
    """
    index = METRIC_INDEX[metric]
    scale = math.inf
    for source, neighbours in graph.adjacency.items():
        for target, weights in neighbours.items():
            crow_km = straight_line_km(graph, source, target)
            if crow_km > _MIN_CALIBRATION_KM:
                scale = min(scale, weights[index] / crow_km)
    # No usable edge -> h(n) = 0, and A* degrades gracefully to Dijkstra.
    return 0.0 if math.isinf(scale) else scale
