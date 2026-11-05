"""Tests for multi-stop pickup-and-delivery routing (pdp.py, multistop_sim.py).

Hand-built graphs use two-way roads whose km and minutes are equal, so
every expected route and total below can be checked by hand.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import math
import unittest

from src.driver import Driver
from src.graph import Graph, Location
from src.multistop_sim import (MultiStopConfig, build_scenario, compare,
                               run_dispatch)
from src.order import Order, OrderStatus
from src.pdp import (AssignmentError, PickupDeliveryPlanner, PlannerConfig,
                     Stop, StopKind, expand_route, validate_route)
from src.routing import DistanceMatrix
from src.serviceability import ServiceabilityConfig, ServiceabilityPolicy
from tests.test_simulation import _real_graph


def _graph(roads: list[tuple[str, str, float]],
           one_way: list[tuple[str, str, float]] = (),
           isolated: tuple[str, ...] = ()) -> Graph:
    """Nodes starting with 'R' are restaurants, all others societies."""
    graph = Graph()
    nodes = sorted({n for a, b, _ in [*roads, *one_way] for n in (a, b)}
                   | set(isolated))
    for node in nodes:
        kind = "Restaurant" if node.startswith("R") else "Society"
        graph.add_location(Location(node, node, kind, "", "Sangli 416416",
                                    16.85, 74.60))
    for a, b, w in roads:
        graph.add_edge(a, b, w, w)
        graph.add_edge(b, a, w, w)
    for a, b, w in one_way:
        graph.add_edge(a, b, w, w)
    return graph


def _planner(graph: Graph, orders: list[Order], mode: str = "insertion",
             **config) -> PickupDeliveryPlanner:
    return PickupDeliveryPlanner(DistanceMatrix(graph, "time"),
                                 {o.order_id: o for o in orders},
                                 PlannerConfig(mode=mode, **config))


def _stops(driver: Driver) -> list[str]:
    return [str(s) for s in driver.route]


def _run_to_end(planner: PickupDeliveryPlanner,
                drivers: list[Driver]) -> None:
    for driver in drivers:
        planner.advance(driver, math.inf)


# Case A graph:  S0 -1- R1 -1- R2 -2- S1
CASE_A = [("S0", "R1", 1), ("R1", "R2", 1), ("R2", "S1", 2)]
# Case B graph:  S0 -1- R1 -1- S1 -1- S2 -1- S3, plus R1 -5- S3
CASE_B = [("S0", "R1", 1), ("R1", "S1", 1), ("S1", "S2", 1),
          ("S2", "S3", 1), ("R1", "S3", 5)]
# Case C graph:  S0 -1- R1 -1- R2 -1- S1 -1- S2 -5- R3 -1- S3
CASE_C = [("S0", "R1", 1), ("R1", "R2", 1), ("R2", "S1", 1),
          ("S1", "S2", 1), ("S2", "R3", 5), ("R3", "S3", 1)]


class CaseAOneSocietyManyRestaurants(unittest.TestCase):
    def test_both_orders_batched_to_shared_society(self) -> None:
        orders = [Order("O1", "R1", "S1"), Order("O2", "R2", "S1")]
        driver = Driver("D1", "S0", capacity=2)
        planner = _planner(_graph(CASE_A), orders)

        applied = planner.assign(orders, [driver], now=0.0)

        self.assertEqual([a.order_id for a in applied], ["O1", "O2"])
        self.assertEqual(_stops(driver), ["P(O1@R1)", "P(O2@R2)",
                                          "D(O2@S1)", "D(O1@S1)"])
        _run_to_end(planner, [driver])
        self.assertEqual([o.status for o in orders],
                         [OrderStatus.DELIVERED] * 2)
        self.assertAlmostEqual(driver.total_distance, 4.0)
        self.assertEqual(driver.road_path, ["S0", "R1", "R2", "S1"])

    def test_baseline_does_not_batch(self) -> None:
        orders = [Order("O1", "R1", "S1"), Order("O2", "R2", "S1")]
        driver = Driver("D1", "S0", capacity=2)
        planner = _planner(_graph(CASE_A), orders, mode="append")
        planner.assign(orders, [driver], now=0.0)
        self.assertEqual(_stops(driver), ["P(O1@R1)", "D(O1@S1)",
                                          "P(O2@R2)", "D(O2@S1)"])
        _run_to_end(planner, [driver])
        self.assertAlmostEqual(driver.total_distance, 8.0)

    def test_not_ready_restaurant_keeps_orders_separate(self) -> None:
        # Waiting for R2 (ready at 20) would deliver O1 at 22 > deadline 10.
        orders = [Order("O1", "R1", "S1", deadline=10.0),
                  Order("O2", "R2", "S1", ready_at=20.0)]
        driver = Driver("D1", "S0", capacity=2)
        planner = _planner(_graph(CASE_A), orders)
        planner.assign(orders, [driver], now=0.0)
        self.assertEqual(_stops(driver), ["P(O1@R1)", "D(O1@S1)",
                                          "P(O2@R2)", "D(O2@S1)"])
        _run_to_end(planner, [driver])
        self.assertAlmostEqual(orders[0].delivered_at, 4.0)
        self.assertAlmostEqual(orders[1].picked_up_at, 20.0)  # waited
        self.assertAlmostEqual(orders[1].delivered_at, 22.0)

    def test_detour_limit_keeps_orders_separate(self) -> None:
        # S0 -1- R1 -1- S1, R1 -3- R2 -3- S1. Batching P1,P2,D2,D1 costs
        # 7 < 8 but O1 rides 6 min against a direct 1 min; the other batch
        # P2,P1,D2,D1 (cost 8) makes O2 ride 4 min against a direct 3 min.
        # With a 0.5 min limit only separate trips remain.
        graph = _graph([("S0", "R1", 1), ("R1", "S1", 1), ("R1", "R2", 3),
                        ("R2", "S1", 3)])
        orders = [Order("O1", "R1", "S1"), Order("O2", "R2", "S1")]

        unlimited = Driver("D1", "S0", capacity=2)
        _planner(graph, copy.deepcopy(orders)).assign(
            copy.deepcopy(orders), [unlimited], now=0.0)
        self.assertEqual(_stops(unlimited), ["P(O1@R1)", "P(O2@R2)",
                                             "D(O2@S1)", "D(O1@S1)"])

        limited = Driver("D1", "S0", capacity=2)
        _planner(graph, orders, max_ride_extra_min=0.5).assign(
            orders, [limited], now=0.0)
        self.assertEqual(_stops(limited), ["P(O1@R1)", "D(O1@S1)",
                                           "P(O2@R2)", "D(O2@S1)"])

    def test_capacity_exceeded_leaves_order_pending(self) -> None:
        orders = [Order("O1", "R1", "S1"), Order("O2", "R2", "S1")]
        driver = Driver("D1", "S0", capacity=1)
        planner = _planner(_graph(CASE_A), orders)

        applied = planner.assign(orders, [driver], now=0.0)

        self.assertEqual([a.order_id for a in applied], ["O1"])
        self.assertEqual(orders[1].status, OrderStatus.PENDING)
        self.assertEqual(driver.remaining_capacity, 0)
        # Once O1 is delivered the driver can take O2.
        planner.advance(driver, math.inf)
        applied = planner.assign([orders[1]], [driver], now=driver.clock)
        self.assertEqual([a.order_id for a in applied], ["O2"])


class CaseBOneRestaurantManySocieties(unittest.TestCase):
    def test_collects_all_then_delivers_in_best_order(self) -> None:
        orders = [Order("O1", "R1", "S3"), Order("O2", "R1", "S1"),
                  Order("O3", "R1", "S2")]
        driver = Driver("D1", "S0", capacity=3)
        planner = _planner(_graph(CASE_B), orders)

        planner.assign(orders, [driver], now=0.0)

        self.assertEqual(_stops(driver), [
            "P(O1@R1)", "P(O3@R1)", "P(O2@R1)",
            "D(O2@S1)", "D(O3@S2)", "D(O1@S3)"])
        _run_to_end(planner, [driver])
        self.assertAlmostEqual(driver.total_distance, 4.0)
        self.assertEqual(driver.road_path, ["S0", "R1", "S1", "S2", "S3"])
        # Every order was picked up before it was delivered.
        for order in orders:
            self.assertEqual(order.status, OrderStatus.DELIVERED)
            self.assertLessEqual(order.picked_up_at, order.delivered_at)

    def test_unready_order_is_not_collected_with_the_rest(self) -> None:
        # O3 is ready only at 30; everyone has a 10-minute deadline from 0
        # except O3, so O3 cannot ride along with O1/O2.
        orders = [Order("O1", "R1", "S3", deadline=10.0),
                  Order("O2", "R1", "S1", deadline=10.0),
                  Order("O3", "R1", "S2", ready_at=30.0)]
        driver = Driver("D1", "S0", capacity=3)
        planner = _planner(_graph(CASE_B), orders)
        planner.assign(orders, [driver], now=0.0)
        route = _stops(driver)
        self.assertEqual(route[:4], ["P(O1@R1)", "P(O2@R1)",
                                     "D(O2@S1)", "D(O1@S3)"])
        self.assertEqual(route[4:], ["P(O3@R1)", "D(O3@S2)"])


class CaseCManyRestaurantsManySocieties(unittest.TestCase):
    def test_interleaved_batch_and_separate_driver(self) -> None:
        orders = [Order("O1", "R1", "S2"), Order("O2", "R2", "S1"),
                  Order("O3", "R3", "S3")]
        near = Driver("DA", "S0", capacity=3)
        far = Driver("DB", "S3", capacity=3)
        planner = _planner(_graph(CASE_C), orders)

        planner.assign(orders, [near, far], now=0.0)

        self.assertEqual(_stops(near), ["P(O1@R1)", "P(O2@R2)",
                                        "D(O2@S1)", "D(O1@S2)"])
        self.assertEqual(_stops(far), ["P(O3@R3)", "D(O3@S3)"])
        _run_to_end(planner, [near, far])
        self.assertAlmostEqual(near.total_distance, 4.0)
        self.assertAlmostEqual(far.total_distance, 2.0)
        # Each order went from its own restaurant to its own society.
        for order in orders:
            driver = near if order.assigned_driver == "DA" else far
            visits = [(s.kind, s.node) for _, s in driver.stop_log
                      if s.order_id == order.order_id]
            self.assertEqual(visits, [(StopKind.PICKUP, order.restaurant_id),
                                      (StopKind.DROPOFF, order.society_id)])

    def test_expanded_route_uses_only_real_edges(self) -> None:
        graph = _graph(CASE_C)
        orders = [Order("O1", "R1", "S2"), Order("O2", "R2", "S1")]
        driver = Driver("DA", "S0", capacity=2)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        path = expand_route(planner.matrix, driver.current_node, driver.route)
        self.assertEqual(path, ["S0", "R1", "R2", "S1", "S2"])
        for u, v in zip(path, path[1:]):
            self.assertIn(v, graph.adjacency[u])


class RouteConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = _graph(CASE_C)
        self.orders = [Order("O1", "R1", "S2"), Order("O2", "R2", "S1")]
        self.planner = _planner(self.graph, self.orders)

    def test_insertion_into_route_with_order_on_board(self) -> None:
        driver = Driver("DA", "S0", capacity=2)
        self.planner.assign([self.orders[0]], [driver], now=0.0)
        self.planner.advance(driver, 1.0)            # O1 picked up at R1
        self.assertEqual(driver.onboard, {"O1"})

        self.planner.assign([self.orders[1]], [driver], now=1.0)

        self.assertEqual(_stops(driver), ["P(O2@R2)", "D(O2@S1)",
                                          "D(O1@S2)"])
        validate_route(driver, self.planner.orders)

    def test_stop_being_driven_to_is_not_moved(self) -> None:
        driver = Driver("DA", "S0", capacity=2)
        self.planner.assign([self.orders[0]], [driver], now=0.0)
        self.planner.advance(driver, 1.5)            # between R1 and S2
        self.planner.assign([self.orders[1]], [driver], now=1.5)
        self.assertEqual(_stops(driver), ["D(O1@S2)", "P(O2@R2)",
                                          "D(O2@S1)"])

    def test_dropoff_before_pickup_is_infeasible(self) -> None:
        driver = Driver("DA", "S0", capacity=2)
        o1 = self.orders[0]
        bad = [Stop(StopKind.DROPOFF, "O1", o1.society_id),
               Stop(StopKind.PICKUP, "O1", o1.restaurant_id)]
        self.assertIsNone(self.planner.evaluate(driver, bad, 0.0))

    def test_duplicate_stops_are_infeasible(self) -> None:
        driver = Driver("DA", "S0", capacity=2)
        twice = [Stop(StopKind.PICKUP, "O1", "R1"),
                 Stop(StopKind.DROPOFF, "O1", "S2"),
                 Stop(StopKind.DROPOFF, "O1", "S2")]
        self.assertIsNone(self.planner.evaluate(driver, twice, 0.0))

    def test_wrong_society_is_rejected_everywhere(self) -> None:
        driver = Driver("DA", "S0", capacity=2)
        wrong = [Stop(StopKind.PICKUP, "O1", "R1"),
                 Stop(StopKind.DROPOFF, "O1", "S1")]    # O1 goes to S2
        self.assertIsNone(self.planner.evaluate(driver, wrong, 0.0))

        self.planner.assign([self.orders[0]], [driver], now=0.0)
        driver.route[1] = Stop(StopKind.DROPOFF, "O1", "S1")  # tamper
        with self.assertRaises(ValueError):
            validate_route(driver, self.planner.orders)
        with self.assertRaises(ValueError):
            self.planner.advance(driver, math.inf)
        self.assertNotEqual(self.orders[0].status, OrderStatus.DELIVERED)

    def test_duplicate_assignment_is_prevented(self) -> None:
        a, b = Driver("DA", "S0", capacity=2), Driver("DB", "S0", capacity=2)
        order = self.orders[0]
        ins_a = self.planner.best_insertion(order, a, 0.0)
        ins_b = self.planner.best_insertion(order, b, 0.0)
        self.planner.apply(ins_a, a, 0.0)
        with self.assertRaises(AssignmentError):
            self.planner.apply(ins_b, b, 0.0)
        with self.assertRaises(AssignmentError):
            self.planner.apply(ins_a, a, 0.0)
        self.assertIsNone(self.planner.best_insertion(order, b, 0.0))
        self.assertEqual(order.assigned_driver, "DA")
        self.assertEqual(b.active_orders, [])

    def test_order_assigned_once_across_many_drivers(self) -> None:
        drivers = [Driver(f"D{i}", "S0", capacity=2) for i in range(5)]
        self.planner.assign(self.orders, drivers, now=0.0)
        holders = [d.driver_id for d in drivers for o in d.active_orders]
        self.assertEqual(len(holders), 2)
        all_active = [o for d in drivers for o in d.active_orders]
        self.assertEqual(sorted(all_active), ["O1", "O2"])

    def test_unreachable_pickup_and_dropoff(self) -> None:
        # R9 has no incoming roads; S9 has no incoming roads.
        graph = _graph(CASE_C, one_way=[("R9", "S1", 1), ("S9", "R1", 1)])
        orders = [Order("O1", "R9", "S1"), Order("O2", "R1", "S9")]
        driver = Driver("DA", "S0", capacity=2)
        planner = _planner(graph, orders)
        self.assertEqual(planner.assign(orders, [driver], now=0.0), [])
        self.assertEqual([o.status for o in orders],
                         [OrderStatus.PENDING] * 2)
        self.assertEqual(driver.route, [])

    def test_unserviceable_pair_is_not_planned(self) -> None:
        policy = ServiceabilityPolicy(self.graph, ServiceabilityConfig(
            denied_pairs={("R1", "S2")}, max_distance_km=None,
            max_time_min=None))
        planner = PickupDeliveryPlanner(
            DistanceMatrix(self.graph, "time"),
            {o.order_id: o for o in self.orders}, serviceability=policy)
        driver = Driver("DA", "S0", capacity=2)
        applied = planner.assign(self.orders, [driver], now=0.0)
        self.assertEqual([a.order_id for a in applied], ["O2"])
        self.assertEqual(self.orders[0].status, OrderStatus.PENDING)

    def test_multiple_orders_sharing_pickup_and_dropoff(self) -> None:
        graph = _graph(CASE_A)
        orders = [Order("O1", "R1", "S1"), Order("O2", "R1", "S1"),
                  Order("O3", "R2", "S1")]
        driver = Driver("D1", "S0", capacity=3)
        planner = _planner(graph, orders)
        planner.assign(orders, [driver], now=0.0)
        validate_route(driver, planner.orders)
        self.assertEqual([s.node for s in driver.route],
                         ["R1", "R1", "R2", "S1", "S1", "S1"])
        _run_to_end(planner, [driver])
        self.assertAlmostEqual(driver.total_distance, 4.0)
        self.assertTrue(all(o.status is OrderStatus.DELIVERED
                            for o in orders))

    def test_no_feasible_batch(self) -> None:
        # Every order's deadline is shorter than its direct trip.
        orders = [Order("O1", "R1", "S2", deadline=1.0),
                  Order("O2", "R2", "S1", deadline=1.0)]
        driver = Driver("DA", "S0", capacity=2)
        planner = _planner(self.graph, orders)
        self.assertEqual(planner.assign(orders, [driver], now=0.0), [])
        self.assertEqual(driver.route, [])

    def test_infeasible_orders_fail_in_simulation(self) -> None:
        orders = [Order("O1", "R1", "S2", deadline=1.0),
                  Order("O2", "R2", "S1")]
        drivers = [Driver("DA", "S0", capacity=2)]
        result = run_dispatch(DistanceMatrix(self.graph, "time"), orders,
                              drivers, "insertion",
                              MultiStopConfig(horizon_min=0.0), None)
        self.assertEqual(orders[0].status, OrderStatus.FAILED)
        self.assertEqual(orders[0].failure_reason, "DEADLINE_UNREACHABLE")
        self.assertEqual(orders[1].status, OrderStatus.DELIVERED)
        self.assertEqual((result.delivered, result.failed), (1, 1))

    def test_deterministic_for_same_input(self) -> None:
        def plan() -> list[list[str]]:
            orders = [Order("O1", "R1", "S2"), Order("O2", "R2", "S1"),
                      Order("O3", "R3", "S3"), Order("O4", "R1", "S1")]
            drivers = [Driver("DA", "S0", capacity=2),
                       Driver("DB", "S3", capacity=2)]
            _planner(self.graph, orders).assign(orders, drivers, now=0.0)
            return [_stops(d) for d in drivers]

        self.assertEqual(plan(), plan())


class RealDataSimulationTests(unittest.TestCase):
    """Baseline vs optimiser on the project dataset (seed 42)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = _real_graph()
        cls.policy = ServiceabilityPolicy.default(cls.graph)
        cls.comparison = compare(cls.graph, MultiStopConfig(seed=42),
                                 cls.policy)

    def test_same_inputs_for_both_runs(self) -> None:
        base, opt = self.comparison.baseline, self.comparison.optimized
        self.assertEqual(
            [(o.order_id, o.restaurant_id, o.society_id, o.created_at,
              o.ready_at, o.deadline) for o in base.orders],
            [(o.order_id, o.restaurant_id, o.society_id, o.created_at,
              o.ready_at, o.deadline) for o in opt.orders])
        self.assertEqual(
            [(d.driver_id, d.capacity) for d in base.drivers],
            [(d.driver_id, d.capacity) for d in opt.drivers])

    def test_all_constraints_hold_in_both_runs(self) -> None:
        for result in (self.comparison.baseline, self.comparison.optimized):
            orders = {o.order_id: o for o in result.orders}
            delivered_by: dict[str, str] = {}
            for driver in result.drivers:
                self.assertEqual(driver.route, [])
                self.assertEqual(driver.active_orders, [])
                load, peak = 0, 0
                for _, stop in driver.stop_log:
                    order = orders[stop.order_id]
                    if stop.kind is StopKind.PICKUP:
                        self.assertEqual(stop.node, order.restaurant_id)
                        load += 1
                    else:
                        self.assertEqual(stop.node, order.society_id)
                        self.assertNotIn(stop.order_id, delivered_by)
                        delivered_by[stop.order_id] = driver.driver_id
                        load -= 1
                    peak = max(peak, load)
                self.assertLessEqual(peak, driver.capacity)
                for u, v in zip(driver.road_path, driver.road_path[1:]):
                    self.assertIn(v, self.graph.adjacency[u])
            for order in result.orders:
                if order.status is OrderStatus.DELIVERED:
                    self.assertEqual(delivered_by[order.order_id],
                                     order.assigned_driver)
                    self.assertLessEqual(order.ready_at, order.picked_up_at)
                    self.assertLessEqual(order.picked_up_at,
                                         order.delivered_at)
                    self.assertLessEqual(order.delivered_at,
                                         order.deadline + 1e-9)
                    self.assertTrue(self.policy.is_serviceable(
                        order.restaurant_id, order.society_id))
            self.assertEqual(result.late_deliveries, 0)

    def test_optimizer_batches_and_baseline_does_not(self) -> None:
        self.assertEqual(self.comparison.baseline.batched_orders, 0)
        self.assertGreater(self.comparison.optimized.batched_orders, 0)

    def test_distance_kpi_seed_42(self) -> None:
        # Measured result for this seed (26.8 %); not a general guarantee.
        self.assertTrue(self.comparison.same_service_level)
        self.assertGreaterEqual(self.comparison.distance_reduction, 0.15)

    def test_p95_dispatch_latency_under_100ms(self) -> None:
        self.assertLess(self.comparison.optimized.p95_latency_ms, 100.0)
        self.assertLess(self.comparison.baseline.p95_latency_ms, 100.0)

    def test_deterministic_end_to_end(self) -> None:
        again = compare(self.graph, MultiStopConfig(seed=42), self.policy)
        for a, b in ((self.comparison.baseline, again.baseline),
                     (self.comparison.optimized, again.optimized)):
            self.assertAlmostEqual(a.total_distance_km, b.total_distance_km,
                                   places=9)
            self.assertEqual([[str(s) for _, s in d.stop_log]
                              for d in a.drivers],
                             [[str(s) for _, s in d.stop_log]
                              for d in b.drivers])

    def test_scenario_is_reproducible(self) -> None:
        cfg = MultiStopConfig(seed=7, num_orders=20, num_drivers=5)
        a, _ = build_scenario(self.graph, cfg, self.policy)
        b, _ = build_scenario(self.graph, cfg, self.policy)
        self.assertEqual([(o.restaurant_id, o.created_at, o.ready_at)
                          for o in a],
                         [(o.restaurant_id, o.created_at, o.ready_at)
                          for o in b])


if __name__ == "__main__":
    unittest.main()
