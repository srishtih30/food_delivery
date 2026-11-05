"""Regression tests for the existing Dijkstra / A* searches and for the
stop-to-stop DistanceMatrix built on top of them.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from src.astar import astar
from src.dijkstra import dijkstra
from src.graph import Graph, Location
from src.heuristics import calibrate_scale
from src.routing import DistanceMatrix
from tests.test_simulation import _real_graph


class ExistingAlgorithmRegressionTests(unittest.TestCase):
    def test_documented_readme_route(self) -> None:
        # README example: R190 -> S010, time metric.
        graph = _real_graph()
        scale = calibrate_scale(graph, "time")
        for result in (dijkstra(graph, "R190", "S010"),
                       astar(graph, "R190", "S010", "time", scale)):
            self.assertEqual(result.path, ["R190", "S001", "S010"])
            self.assertAlmostEqual(result.total_time_min, 18.3, places=6)
            self.assertAlmostEqual(result.total_distance_km, 3.018, places=6)

    def test_documented_demo_pair(self) -> None:
        # README: R128 -> S019, Dijkstra 197 nodes, A* 167, both 45.1 min.
        graph = _real_graph()
        d = dijkstra(graph, "R128", "S019")
        a = astar(graph, "R128", "S019", "time", calibrate_scale(graph, "time"))
        self.assertAlmostEqual(d.cost, 45.1, places=6)
        self.assertAlmostEqual(a.cost, 45.1, places=6)
        self.assertEqual((d.nodes_visited, a.nodes_visited), (197, 167))

    def test_astar_matches_dijkstra_on_sampled_pairs(self) -> None:
        graph = _real_graph()
        ids = sorted(graph.locations)
        for metric in ("time", "distance"):
            scale = calibrate_scale(graph, metric)
            for source in ids[::17]:
                for target in ids[::5]:
                    if source == target:
                        continue
                    d = dijkstra(graph, source, target, metric)
                    a = astar(graph, source, target, metric, scale)
                    self.assertAlmostEqual(d.cost, a.cost, places=9)

    def test_no_route(self) -> None:
        graph = Graph()
        for node in ("A", "B"):
            graph.add_location(Location(node, node, "Society", "", "", 0, 0))
        graph.add_edge("A", "B", 1.0, 1.0)      # one-way only
        self.assertFalse(dijkstra(graph, "B", "A").found)
        self.assertFalse(astar(graph, "B", "A", "time", 0.0).found)


class DistanceMatrixTests(unittest.TestCase):
    def test_tree_backend_matches_existing_algorithms(self) -> None:
        graph = _real_graph()
        ids = sorted(graph.locations)
        for metric in ("time", "distance"):
            tree = DistanceMatrix(graph, metric, "tree")
            for algorithm in ("dijkstra", "astar"):
                other = DistanceMatrix(graph, metric, algorithm)
                for source in ids[::23]:
                    for target in ids[::7]:
                        a, b = tree.leg(source, target), other.leg(source,
                                                                   target)
                        self.assertIsNotNone(a)
                        self.assertAlmostEqual(a.cost, b.cost, places=9)

    def test_leg_totals_and_path_use_real_edges(self) -> None:
        graph = _real_graph()
        matrix = DistanceMatrix(graph, "time")
        leg = matrix.leg("R190", "S010")
        self.assertEqual(leg.path, ("R190", "S001", "S010"))
        self.assertAlmostEqual(leg.time_min, 18.3, places=6)
        self.assertAlmostEqual(leg.distance_km, 3.018, places=6)
        for u, v in zip(leg.path, leg.path[1:]):
            self.assertIn(v, graph.adjacency[u])

    def test_unreachable_and_unknown(self) -> None:
        graph = Graph()
        for node in ("A", "B"):
            graph.add_location(Location(node, node, "Society", "", "", 0, 0))
        graph.add_edge("A", "B", 1.0, 2.0)
        for algorithm in DistanceMatrix.ALGORITHMS:
            matrix = DistanceMatrix(graph, "time", algorithm)
            self.assertIsNone(matrix.leg("B", "A"))
            self.assertIsNone(matrix.leg("A", "Z"))
            self.assertEqual(matrix.leg("A", "B").path, ("A", "B"))
            self.assertEqual(matrix.leg("A", "A").cost, 0.0)

    def test_invalidate_recomputes(self) -> None:
        graph = Graph()
        for node in ("A", "B", "C"):
            graph.add_location(Location(node, node, "Society", "", "", 0, 0))
        graph.add_edge("A", "B", 1.0, 1.0)
        graph.add_edge("B", "C", 1.0, 1.0)
        matrix = DistanceMatrix(graph, "time")
        self.assertEqual(matrix.leg("A", "C").path, ("A", "B", "C"))
        graph.add_edge("A", "C", 1.0, 1.0)
        matrix.invalidate()
        self.assertEqual(matrix.leg("A", "C").path, ("A", "C"))


if __name__ == "__main__":
    unittest.main()
