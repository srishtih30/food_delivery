"""Tests for the delivery simulation / greedy dispatch baseline.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import functools
import os
import unittest

from src.data_loader import load_dataset
from src.dispatch import DispatchEngine
from src.driver import Driver, DriverStatus, generate_drivers
from src.graph import Graph, Location, build_graph
from src.order import Order, OrderStatus, generate_orders
from src.simulation import DeliverySimulation

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


@functools.lru_cache(maxsize=1)
def _real_graph() -> Graph:
    """The project's real dataset, loaded once and shared by every test."""
    return build_graph(load_dataset(DATA_DIR))


def _tiny_graph() -> Graph:
    """A minimal, fully-connected graph for fast, isolated dispatch tests:
    two societies and one restaurant, all mutually reachable."""
    graph = Graph()
    graph.add_location(Location("S1", "Society One", "Society", "A", "addr",
                                0.0000, 0.0000))
    graph.add_location(Location("S2", "Society Two", "Society", "A", "addr",
                                0.0200, 0.0200))
    graph.add_location(Location("R1", "Restaurant One", "Restaurant", "A",
                                "addr", 0.0050, 0.0050))
    for a, b, km, minutes in [
        ("S1", "R1", 1.0, 5.0), ("R1", "S1", 1.0, 5.0),
        ("R1", "S2", 2.0, 8.0), ("S2", "R1", 2.0, 8.0),
        ("S1", "S2", 3.0, 10.0), ("S2", "S1", 3.0, 10.0),
    ]:
        graph.add_edge(a, b, km, minutes)
    return graph


class DriverGenerationTests(unittest.TestCase):
    def test_generates_exactly_fifty_drivers_by_default_count(self) -> None:
        drivers = generate_drivers(_real_graph(), 50, seed=42)
        self.assertEqual(len(drivers), 50)
        self.assertEqual(len({d.driver_id for d in drivers}), 50)

    def test_drivers_start_on_real_connected_graph_nodes(self) -> None:
        graph = _real_graph()
        for driver in generate_drivers(graph, 50, seed=42):
            self.assertIn(driver.current_node, graph.locations)
            self.assertTrue(graph.is_connected_node(driver.current_node))

    def test_drivers_start_available_and_idle(self) -> None:
        for driver in generate_drivers(_real_graph(), 10, seed=1):
            self.assertEqual(driver.status, DriverStatus.AVAILABLE)
            self.assertIsNone(driver.assigned_order)
            self.assertEqual(driver.completed_orders, 0)

    def test_fixed_seed_is_reproducible(self) -> None:
        a = generate_drivers(_real_graph(), 50, seed=42)
        b = generate_drivers(_real_graph(), 50, seed=42)
        self.assertEqual([d.current_node for d in a],
                         [d.current_node for d in b])


class OrderGenerationTests(unittest.TestCase):
    def test_generates_exactly_hundred_orders_by_default_count(self) -> None:
        orders = generate_orders(_real_graph(), 100, seed=42)
        self.assertEqual(len(orders), 100)
        self.assertEqual(len({o.order_id for o in orders}), 100)

    def test_order_nodes_exist_in_graph_with_correct_kind(self) -> None:
        graph = _real_graph()
        for order in generate_orders(graph, 100, seed=42):
            self.assertIn(order.restaurant_id, graph.locations)
            self.assertIn(order.society_id, graph.locations)
            self.assertEqual(graph.locations[order.restaurant_id].kind,
                             "Restaurant")
            self.assertEqual(graph.locations[order.society_id].kind,
                             "Society")

    def test_every_order_has_a_valid_restaurant_to_society_route(self) -> None:
        from src.dijkstra import dijkstra
        graph = _real_graph()
        for order in generate_orders(graph, 100, seed=42):
            result = dijkstra(graph, order.restaurant_id, order.society_id)
            self.assertTrue(result.found,
                            f"no route for order {order.order_id}")

    def test_orders_start_pending(self) -> None:
        for order in generate_orders(_real_graph(), 20, seed=7):
            self.assertEqual(order.status, OrderStatus.PENDING)
            self.assertIsNone(order.assigned_driver)

    def test_fixed_seed_is_reproducible(self) -> None:
        a = generate_orders(_real_graph(), 100, seed=42)
        b = generate_orders(_real_graph(), 100, seed=42)
        self.assertEqual([(o.restaurant_id, o.society_id) for o in a],
                         [(o.restaurant_id, o.society_id) for o in b])


class OrderStateMachineTests(unittest.TestCase):
    def test_legal_transition_sequence(self) -> None:
        order = Order("O0001", "R1", "S1")
        order.transition(OrderStatus.ASSIGNED)
        order.transition(OrderStatus.PICKED_UP)
        order.transition(OrderStatus.DELIVERED)
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_pending_to_delivered_is_illegal(self) -> None:
        order = Order("O0001", "R1", "S1")
        with self.assertRaises(ValueError):
            order.transition(OrderStatus.DELIVERED)

    def test_assigned_to_delivered_without_pickup_is_illegal(self) -> None:
        order = Order("O0001", "R1", "S1")
        order.transition(OrderStatus.ASSIGNED)
        with self.assertRaises(ValueError):
            order.transition(OrderStatus.DELIVERED)

    def test_delivered_is_terminal(self) -> None:
        order = Order("O0001", "R1", "S1")
        order.transition(OrderStatus.ASSIGNED)
        order.transition(OrderStatus.PICKED_UP)
        order.transition(OrderStatus.DELIVERED)
        with self.assertRaises(ValueError):
            order.transition(OrderStatus.PICKED_UP)


class DispatchEngineTests(unittest.TestCase):
    def test_dispatch_assigns_nearest_available_driver(self) -> None:
        graph = _tiny_graph()
        near = Driver("D1", "S1")
        far = Driver("D2", "S2")
        order = Order("O0001", "R1", "S2")
        engine = DispatchEngine(graph, algorithm="dijkstra", metric="time")

        delivered = engine.dispatch_order(order, [near, far])

        self.assertTrue(delivered)
        self.assertEqual(order.assigned_driver, "D1")
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_driver_location_updates_to_restaurant_then_society(self) -> None:
        graph = _tiny_graph()
        driver = Driver("D1", "S1")
        order = Order("O0001", "R1", "S2")
        engine = DispatchEngine(graph, algorithm="dijkstra", metric="time")

        engine.dispatch_order(order, [driver])

        self.assertEqual(driver.current_node, "S2")  # ends at the society
        self.assertEqual(order.pickup_route[-1], "R1")
        self.assertEqual(order.delivery_route[-1], "S2")

    def test_pickup_happens_before_delivery(self) -> None:
        graph = _tiny_graph()
        driver = Driver("D1", "S1")
        order = Order("O0001", "R1", "S2")
        engine = DispatchEngine(graph, algorithm="dijkstra", metric="time")

        engine.dispatch_order(order, [driver])

        self.assertIsNotNone(order.pickup_time)
        self.assertIsNotNone(order.delivery_time)
        self.assertLessEqual(order.pickup_time, order.delivery_time)
        self.assertTrue(order.pickup_route)
        self.assertTrue(order.delivery_route)

    def test_driver_becomes_available_after_release(self) -> None:
        graph = _tiny_graph()
        driver = Driver("D1", "S1")
        order = Order("O0001", "R1", "S2")
        engine = DispatchEngine(graph, algorithm="dijkstra", metric="time")

        engine.dispatch_order(order, [driver])
        self.assertEqual(driver.status, DriverStatus.AVAILABLE_AFTER_DELIVERY)

        engine.release_completed_drivers([driver])
        self.assertEqual(driver.status, DriverStatus.AVAILABLE)
        self.assertEqual(driver.completed_orders, 1)
        self.assertIsNone(driver.assigned_order)

    def test_no_available_driver_leaves_order_pending(self) -> None:
        graph = _tiny_graph()
        busy = Driver("D1", "S1", status=DriverStatus.TO_PICKUP)
        order = Order("O0001", "R1", "S2")
        engine = DispatchEngine(graph, algorithm="dijkstra", metric="time")

        delivered = engine.dispatch_order(order, [busy])

        self.assertFalse(delivered)
        self.assertEqual(order.status, OrderStatus.PENDING)

    def test_dispatch_rejects_non_pending_order(self) -> None:
        graph = _tiny_graph()
        driver = Driver("D1", "S1")
        order = Order("O0001", "R1", "S2", status=OrderStatus.ASSIGNED)
        engine = DispatchEngine(graph, algorithm="dijkstra", metric="time")

        with self.assertRaises(ValueError):
            engine.dispatch_order(order, [driver])


class DeliverySimulationTests(unittest.TestCase):
    def test_runs_fifty_drivers_hundred_orders(self) -> None:
        sim = DeliverySimulation(_real_graph(), num_drivers=50,
                                 num_orders=100, seed=42, algorithm="astar")
        metrics = sim.run()
        self.assertEqual(len(sim.drivers), 50)
        self.assertEqual(len(sim.orders), 100)
        self.assertEqual(metrics.total_orders, 100)

    def test_all_orders_eventually_delivered(self) -> None:
        sim = DeliverySimulation(_real_graph(), num_drivers=50,
                                 num_orders=100, seed=42, algorithm="astar")
        metrics = sim.run()
        self.assertEqual(metrics.delivered_orders + metrics.failed_orders,
                         metrics.total_orders)
        self.assertEqual(metrics.delivered_orders, metrics.total_orders,
                         "every order should be deliverable on this "
                         "connected dataset")

    def test_no_delivered_order_skipped_pickup(self) -> None:
        sim = DeliverySimulation(_real_graph(), num_drivers=10,
                                 num_orders=30, seed=7, algorithm="dijkstra")
        sim.run()
        for order in sim.orders:
            if order.status is OrderStatus.DELIVERED:
                self.assertTrue(order.pickup_route)
                self.assertIsNotNone(order.pickup_time)
                self.assertLessEqual(order.pickup_time, order.delivery_time)

    def test_dijkstra_and_astar_are_both_selectable(self) -> None:
        for algorithm in ("dijkstra", "astar"):
            sim = DeliverySimulation(_real_graph(), num_drivers=5,
                                     num_orders=5, seed=1,
                                     algorithm=algorithm)
            metrics = sim.run()
            self.assertEqual(metrics.total_orders, 5)

    def test_fixed_seed_reproducible_end_to_end(self) -> None:
        run_a = DeliverySimulation(_real_graph(), num_drivers=20,
                                   num_orders=40, seed=42).run()
        run_b = DeliverySimulation(_real_graph(), num_drivers=20,
                                   num_orders=40, seed=42).run()
        self.assertEqual(run_a.delivered_orders, run_b.delivered_orders)
        self.assertAlmostEqual(run_a.total_distance_km,
                               run_b.total_distance_km, places=6)
        self.assertAlmostEqual(run_a.total_travel_time_min,
                               run_b.total_travel_time_min, places=6)


if __name__ == "__main__":
    unittest.main()
