"""Tests for road closures and dynamic rerouting.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import unittest

from src.driver import Driver
from src.multistop_sim import (ClosureEvent, DispatchRun, MultiStopConfig,
                               build_scenario, check_compliance)
from src.order import Order, OrderStatus
from src.routing import DistanceMatrix
from src.serviceability import ServiceabilityPolicy
from tests.test_multistop import CASE_C, _graph, _planner, _stops
from tests.test_simulation import _real_graph

# S0 -1- R1 -1- S1, plus a detour R1 -2- S2 -2- S1
DETOUR = [("S0", "R1", 1), ("R1", "S1", 1), ("R1", "S2", 2), ("S2", "S1", 2)]


def _entered(driver: Driver, road: tuple[str, str]) -> list[float]:
    return [t for t, u, v in driver.road_log if (u, v) == road]


class GraphClosureTests(unittest.TestCase):
    def test_close_and_reopen_restore_weights_and_order(self) -> None:
        graph = _graph(DETOUR)
        before = {n: list(nbrs.items()) for n, nbrs in graph.adjacency.items()}
        weights = graph.close_road("R1", "S1")
        self.assertEqual(weights, (1.0, 1.0))
        self.assertFalse(graph.has_edge("R1", "S1"))
        self.assertTrue(graph.has_edge("S1", "R1"))      # other direction
        graph.reopen_road("R1", "S1")
        self.assertEqual({n: list(nbrs.items())
                          for n, nbrs in graph.adjacency.items()}, before)
        self.assertEqual(graph.closed_roads, {})

    def test_cannot_close_missing_or_closed_road(self) -> None:
        graph = _graph(DETOUR)
        with self.assertRaises(ValueError):
            graph.close_road("S0", "S1")                 # no such road
        graph.close_road("R1", "S1")
        with self.assertRaises(ValueError):
            graph.close_road("R1", "S1")
        with self.assertRaises(ValueError):
            graph.reopen_road("S0", "R1")                # never closed

    def test_matrix_drops_only_affected_legs(self) -> None:
        graph = _graph(DETOUR)
        matrix = DistanceMatrix(graph, "time")
        self.assertEqual(matrix.leg("R1", "S1").path, ("R1", "S1"))
        unaffected = matrix.leg("S0", "R1")
        graph.close_road("R1", "S1")
        matrix.road_closed("R1", "S1")
        self.assertEqual(matrix.leg("R1", "S1").path, ("R1", "S2", "S1"))
        self.assertIs(matrix.leg("S0", "R1"), unaffected)
        graph.reopen_road("R1", "S1")
        matrix.road_reopened()
        self.assertEqual(matrix.leg("R1", "S1").path, ("R1", "S1"))

    def test_existing_algorithms_respect_closure(self) -> None:
        from src.astar import astar
        from src.dijkstra import dijkstra
        graph = _graph(DETOUR)
        graph.close_road("R1", "S1")
        for result in (dijkstra(graph, "S0", "S1"),
                       astar(graph, "S0", "S1", "time", 0.0)):
            self.assertEqual(result.path, ["S0", "R1", "S2", "S1"])
            self.assertAlmostEqual(result.cost, 5.0)


class RerouteTests(unittest.TestCase):
    def test_alternate_route_avoids_closed_road(self) -> None:
        orders = [Order("O1", "R1", "S1")]
        driver = Driver("D1", "S0", capacity=1)
        planner = _planner(_graph(DETOUR), orders)
        planner.assign(orders, [driver], now=0.0)

        report = planner.close_road("R1", "S1", [driver], now=0.0)

        self.assertEqual(report.affected_drivers, ["D1"])
        self.assertEqual(report.failed_orders, {})
        self.assertEqual(_stops(driver), ["P(O1@R1)", "D(O1@S1)"])
        planner.advance(driver, math.inf)
        self.assertEqual(orders[0].status, OrderStatus.DELIVERED)
        self.assertEqual(driver.road_path, ["S0", "R1", "S2", "S1"])
        self.assertEqual(_entered(driver, ("R1", "S1")), [])
        self.assertAlmostEqual(driver.total_distance, 5.0)
        self.assertGreater(report.latency_s, 0.0)

    def test_no_alternate_before_pickup_releases_order(self) -> None:
        # S1 is reachable only through R1 -> S1.
        graph = _graph([("S0", "R1", 1)], one_way=[("R1", "S1", 1),
                                                  ("S1", "S0", 1)])
        orders = [Order("O1", "R1", "S1")]
        driver = Driver("D1", "S0", capacity=1)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)

        report = planner.close_road("R1", "S1", [driver], now=0.0)

        self.assertEqual(report.released_orders,
                         {"O1": "ROAD_CLOSED_UNREACHABLE"})
        self.assertEqual(orders[0].status, OrderStatus.PENDING)
        self.assertIsNone(orders[0].assigned_driver)
        self.assertEqual(driver.route, [])
        self.assertEqual(driver.active_orders, [])
        # It cannot be planned again while the road is closed.
        self.assertEqual(planner.assign(orders, [driver], now=0.0), [])

    def test_no_alternate_after_pickup_fails_order(self) -> None:
        graph = _graph([("S0", "R1", 1)], one_way=[("R1", "S1", 1),
                                                  ("S1", "S0", 1)])
        orders = [Order("O1", "R1", "S1")]
        driver = Driver("D1", "S0", capacity=1)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        planner.advance(driver, 1.0)                     # picked up at R1
        self.assertEqual(driver.onboard, {"O1"})

        report = planner.close_road("R1", "S1", [driver], now=1.0)

        self.assertEqual(report.failed_orders, {"O1": "DROPOFF_UNREACHABLE"})
        self.assertEqual(orders[0].status, OrderStatus.FAILED)
        self.assertIsNone(orders[0].delivered_at)
        self.assertEqual(driver.onboard, set())
        planner.advance(driver, math.inf)
        self.assertNotEqual(orders[0].status, OrderStatus.DELIVERED)

    def test_closure_on_multistop_route_keeps_order_and_capacity(self) -> None:
        # Case C graph plus a detour R2 -3- S2.
        graph = _graph([*CASE_C, ("R2", "S2", 3)])
        orders = [Order("O1", "R1", "S2"), Order("O2", "R2", "S1")]
        driver = Driver("DA", "S0", capacity=2)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        self.assertEqual(_stops(driver), ["P(O1@R1)", "P(O2@R2)",
                                          "D(O2@S1)", "D(O1@S2)"])
        planner.advance(driver, 1.5)          # O1 on board, driving R1->R2

        report = planner.close_road("R2", "S1", [driver], now=1.5)

        # Commitment rule: the driver finishes the road it is on (R1->R2)
        # and the stop it was heading to (P(O2@R2)) is unchanged.
        self.assertEqual(report.reanchored, {"DA": "R2"})
        self.assertEqual(_stops(driver), ["P(O2@R2)", "D(O2@S1)",
                                          "D(O1@S2)"])
        planner.advance(driver, math.inf)
        for order in orders:
            self.assertEqual(order.status, OrderStatus.DELIVERED)
            self.assertLess(order.picked_up_at, order.delivered_at)
        self.assertEqual(driver.road_path,
                         ["S0", "R1", "R2", "S2", "S1", "S2"])
        self.assertEqual(_entered(driver, ("R2", "S1")), [])
        for (_, u, v), (_, u2, _) in zip(driver.road_log,
                                         driver.road_log[1:]):
            self.assertEqual(v, u2)            # continuous, no teleporting
        visits = [(s.order_id, s.node) for _, s in driver.stop_log]
        self.assertEqual(visits, [("O1", "R1"), ("O2", "R2"),
                                  ("O2", "S1"), ("O1", "S2")])

    def test_driver_on_closed_road_finishes_it(self) -> None:
        graph = _graph([*CASE_C, ("R1", "S1", 3)])
        orders = [Order("O1", "R2", "S1")]
        driver = Driver("DA", "S0", capacity=1)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        planner.advance(driver, 1.5)          # on R1 -> R2 since t=1

        report = planner.close_road("R1", "R2", [driver], now=1.5)

        self.assertEqual(report.reanchored, {"DA": "R2"})
        self.assertEqual(_entered(driver, ("R1", "R2")), [1.0])  # pre-closure
        planner.advance(driver, math.inf)
        self.assertEqual(orders[0].status, OrderStatus.DELIVERED)

    def test_unaffected_driver_is_untouched(self) -> None:
        graph = _graph(DETOUR)
        orders = [Order("O1", "R1", "S1")]
        driver = Driver("D1", "S0", capacity=1)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        report = planner.close_road("S2", "S1", [driver], now=0.0)
        self.assertEqual(report.affected_drivers, [])
        planner.advance(driver, math.inf)
        self.assertEqual(driver.road_path, ["S0", "R1", "S1"])

    def test_reopen_restores_shorter_route(self) -> None:
        graph = _graph(DETOUR)
        orders = [Order("O1", "R1", "S1")]
        driver = Driver("D1", "S0", capacity=1)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        planner.close_road("R1", "S1", [driver], now=0.0)
        planner.reopen_road("R1", "S1", [driver], now=0.0)
        planner.advance(driver, math.inf)
        self.assertEqual(driver.road_path, ["S0", "R1", "S1"])


class SimulationClosureTests(unittest.TestCase):
    def test_closures_in_real_simulation_are_compliant_and_restored(
            self) -> None:
        graph = _real_graph()
        before = {n: dict(nbrs) for n, nbrs in graph.adjacency.items()}
        policy = ServiceabilityPolicy.default(graph)
        config = MultiStopConfig(closures=(
            ClosureEvent(20.0), ClosureEvent(30.0, reopen_at=50.0),
            ClosureEvent(40.0)))
        matrix = DistanceMatrix(graph, "time")
        orders, drivers = build_scenario(graph, config, policy)
        result = DispatchRun(matrix, orders, drivers, "insertion", config,
                             policy).finish()

        closes = [r for r in result.reroutes if r.action == "close"]
        self.assertEqual(len(closes), 3)
        self.assertTrue(any(r.affected_drivers for r in closes))
        self.assertEqual(check_compliance(result, policy,
                                          config.max_ride_extra_min, matrix),
                         [])
        self.assertEqual(graph.closed_roads, {})
        self.assertEqual({n: dict(nbrs) for n, nbrs in
                          graph.adjacency.items()}, before)
        for order in result.orders:
            self.assertIn(order.status, (OrderStatus.DELIVERED,
                                         OrderStatus.FAILED))

    def test_roads_restored_even_if_run_fails(self) -> None:
        graph = _graph(DETOUR)
        config = MultiStopConfig(horizon_min=0.0, max_delivery_min=None,
                                 closures=(ClosureEvent(0.0, "R1", "S1"),))
        run = DispatchRun(DistanceMatrix(graph, "time"),
                          [Order("O1", "R1", "S1")],
                          [Driver("D1", "S0", capacity=1)], "insertion",
                          config, None)
        run.step()
        self.assertIn(("R1", "S1"), graph.closed_roads)
        run.planner.advance = None              # force finish() to fail
        with self.assertRaises(TypeError):
            run.finish()
        self.assertEqual(graph.closed_roads, {})


if __name__ == "__main__":
    unittest.main()
