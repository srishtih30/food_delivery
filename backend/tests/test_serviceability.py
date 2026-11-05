"""Tests for restaurant -> society serviceability.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from src.dijkstra import dijkstra
from src.dispatch import DispatchEngine
from src.driver import Driver, DriverStatus
from src.graph import Graph, Location
from src.order import Order, OrderStatus, generate_orders
from src.serviceability import (Reason, ServiceabilityConfig,
                                ServiceabilityPolicy)
from src.simulation import DeliverySimulation
from tests.test_simulation import _real_graph

SANGLI = "Main Road, Sangli 416416, Maharashtra"
MIRAJ = "Station Road, Miraj 416410, Maharashtra"


def _zoned_graph() -> Graph:
    """Synthetic graph with Sangli and Miraj locations.

    RS1 (Sangli) -> SS1 (Sangli): 2 km / 8 min, local delivery
    RM1 (Miraj)  -> SS1 (Sangli): 3 km / 10 min, cross-area but short
    RS2 (Sangli) -> SS2 (Sangli): 12 km / 20 min, too far
    RS3 (Sangli) -> SS3 (Sangli): 5 km / 45 min, too slow
    RS4 (Sangli): no roads, so unreachable
    RX1 (no town in address) -> SS1
    RA1 (Miraj) shares the Area label "Shared Area" with SS1
    """
    graph = Graph()

    def add(node_id: str, kind: str, address: str,
            area: str = "Area") -> None:
        graph.add_location(Location(node_id, node_id, kind, area, address,
                                    16.85, 74.60))

    add("SS1", "Society", SANGLI, area="Shared Area")
    add("SS2", "Society", SANGLI)
    add("SS3", "Society", SANGLI)
    add("RS1", "Restaurant", SANGLI)
    add("RS2", "Restaurant", SANGLI)
    add("RS3", "Restaurant", SANGLI)
    add("RS4", "Restaurant", SANGLI)
    add("RM1", "Restaurant", MIRAJ)
    add("RX1", "Restaurant", "Unnamed lane, Maharashtra")
    add("RA1", "Restaurant", MIRAJ, area="Shared Area")
    for source, target, km, minutes in [
        ("RS1", "SS1", 2.0, 8.0), ("SS1", "RS1", 2.0, 8.0),
        ("RM1", "SS1", 3.0, 10.0), ("SS1", "RM1", 3.0, 10.0),
        ("RS2", "SS2", 12.0, 20.0), ("SS2", "RS2", 12.0, 20.0),
        ("RS3", "SS3", 5.0, 45.0), ("SS3", "RS3", 5.0, 45.0),
        ("RX1", "SS1", 1.0, 4.0), ("RA1", "SS1", 1.0, 4.0),
    ]:
        graph.add_edge(source, target, km, minutes)
    return graph


def _policy(**overrides) -> ServiceabilityPolicy:
    config = ServiceabilityConfig(max_distance_km=8.0, max_time_min=30.0)
    for key, value in overrides.items():
        setattr(config, key, value)
    return ServiceabilityPolicy(_zoned_graph(), config)


class ZoneTests(unittest.TestCase):
    def test_zone_comes_from_postal_town_in_address(self) -> None:
        policy = _policy()
        self.assertEqual(policy.zone_of("RS1"), "Sangli")
        self.assertEqual(policy.zone_of("RM1"), "Miraj")
        self.assertIsNone(policy.zone_of("RX1"))

    def test_zone_override(self) -> None:
        policy = _policy(zone_overrides={"RX1": "Sangli"})
        self.assertEqual(policy.zone_of("RX1"), "Sangli")
        self.assertTrue(policy.is_serviceable("RX1", "SS1"))


class ServiceabilityRuleTests(unittest.TestCase):
    def test_local_delivery_within_limits_is_allowed(self) -> None:
        result = _policy().check("RS1", "SS1")
        self.assertTrue(result.serviceable)
        self.assertIsNone(result.reason)
        self.assertEqual(result.route, ("RS1", "SS1"))
        self.assertAlmostEqual(result.distance_km, 2.0)
        self.assertAlmostEqual(result.time_min, 8.0)

    def test_cross_area_rejected_even_though_route_exists(self) -> None:
        policy = _policy()
        self.assertTrue(dijkstra(policy.graph, "RM1", "SS1").found)
        result = policy.check("RM1", "SS1")
        self.assertFalse(result.serviceable)
        self.assertEqual(result.reason, Reason.OUTSIDE_SERVICE_ZONE)

    def test_same_area_label_does_not_grant_service(self) -> None:
        # RA1 (Miraj) and SS1 (Sangli) share the Area label "Shared Area".
        result = _policy().check("RA1", "SS1")
        self.assertEqual(result.reason, Reason.OUTSIDE_SERVICE_ZONE)

    def test_explicitly_allowed_cross_area_pair_is_accepted(self) -> None:
        result = _policy(allowed_pairs={("RM1", "SS1")}).check("RM1", "SS1")
        self.assertTrue(result.serviceable)

    def test_allowed_zone_pair_is_accepted(self) -> None:
        policy = _policy(allowed_zone_pairs={("Miraj", "Sangli")})
        self.assertTrue(policy.is_serviceable("RM1", "SS1"))

    def test_allowed_zone_pair_is_directional(self) -> None:
        policy = _policy(allowed_zone_pairs={("Sangli", "Miraj")})
        self.assertFalse(policy.is_serviceable("RM1", "SS1"))

    def test_explicitly_allowed_pair_still_obeys_limits(self) -> None:
        policy = _policy(allowed_pairs={("RM1", "SS1")}, max_time_min=9.0)
        result = policy.check("RM1", "SS1")
        self.assertEqual(result.reason, Reason.DELIVERY_TIME_EXCEEDED)

    def test_deny_list_overrides_same_zone(self) -> None:
        result = _policy(denied_pairs={("RS1", "SS1")}).check("RS1", "SS1")
        self.assertEqual(result.reason, Reason.OUTSIDE_SERVICE_ZONE)

    def test_same_zone_can_be_disabled(self) -> None:
        result = _policy(allow_same_zone=False).check("RS1", "SS1")
        self.assertEqual(result.reason, Reason.OUTSIDE_SERVICE_ZONE)

    def test_location_without_zone_is_rejected(self) -> None:
        result = _policy().check("RX1", "SS1")
        self.assertEqual(result.reason, Reason.OUTSIDE_SERVICE_ZONE)

    def test_unreachable_restaurant(self) -> None:
        result = _policy().check("RS4", "SS1")
        self.assertEqual(result.reason, Reason.NO_ROAD_ROUTE)

    def test_distance_limit(self) -> None:
        result = _policy().check("RS2", "SS2")
        self.assertEqual(result.reason, Reason.DELIVERY_DISTANCE_EXCEEDED)
        self.assertAlmostEqual(result.distance_km, 12.0)

    def test_time_limit(self) -> None:
        result = _policy().check("RS3", "SS3")
        self.assertEqual(result.reason, Reason.DELIVERY_TIME_EXCEEDED)
        self.assertAlmostEqual(result.time_min, 45.0)

    def test_limits_can_be_disabled(self) -> None:
        policy = _policy(max_distance_km=None, max_time_min=None)
        self.assertTrue(policy.is_serviceable("RS2", "SS2"))
        self.assertTrue(policy.is_serviceable("RS3", "SS3"))

    def test_unknown_ids(self) -> None:
        policy = _policy()
        for restaurant, society in [("R999", "SS1"), ("RS1", "S999"),
                                    ("R999", "S999")]:
            self.assertEqual(policy.check(restaurant, society).reason,
                             Reason.INVALID_RESTAURANT_OR_SOCIETY)

    def test_wrong_location_kind(self) -> None:
        policy = _policy()
        self.assertEqual(policy.check("SS1", "SS2").reason,
                         Reason.INVALID_RESTAURANT_OR_SOCIETY)
        self.assertEqual(policy.check("RS1", "RS2").reason,
                         Reason.INVALID_RESTAURANT_OR_SOCIETY)

    def test_disabled_restaurant(self) -> None:
        result = _policy(disabled_restaurants={"RS1"}).check("RS1", "SS1")
        self.assertEqual(result.reason,
                         Reason.RESTAURANT_NOT_ACCEPTING_ORDERS)


class ConfigTests(unittest.TestCase):
    def test_from_dict(self) -> None:
        config = ServiceabilityConfig.from_dict({
            "allowed_zone_pairs": [["Miraj", "Sangli"]],
            "allowed_pairs": [["rm1", "ss1"]],
            "disabled_restaurants": ["rs1"],
            "max_distance_km": None,
            "max_time_min": 15,
        })
        self.assertEqual(config.allowed_zone_pairs, {("Miraj", "Sangli")})
        self.assertEqual(config.allowed_pairs, {("RM1", "SS1")})
        self.assertEqual(config.disabled_restaurants, {"RS1"})
        self.assertIsNone(config.max_distance_km)
        self.assertEqual(config.max_time_min, 15)

    def test_shipped_config_file_loads(self) -> None:
        config = ServiceabilityConfig.from_json()
        self.assertEqual(set(config.zones), {"Sangli", "Miraj", "Kupwad"})
        self.assertNotIn(("Miraj", "Sangli"), config.allowed_zone_pairs)

    def test_shipped_config_matches_built_in_defaults(self) -> None:
        # ServiceabilityPolicy(graph) and ServiceabilityPolicy.default(graph)
        # must describe the same policy.
        self.assertEqual(ServiceabilityConfig.from_json(),
                         ServiceabilityConfig())

    def test_unknown_setting_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ServiceabilityConfig.from_dict({"max_time_mins": 10})

    def test_comment_keys_are_ignored(self) -> None:
        ServiceabilityConfig.from_dict({"_comment": "synthetic"})

    def test_unknown_zone_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ServiceabilityConfig.from_dict(
                {"allowed_zone_pairs": [["sangli", "Miraj"]]})
        with self.assertRaises(ValueError):
            ServiceabilityConfig.from_dict({"zone_overrides": {"R1": "Pune"}})

    def test_invalid_limits_and_metric_are_rejected(self) -> None:
        for bad in ({"max_time_min": -1}, {"max_distance_km": "8"},
                    {"route_metric": "cost"}):
            with self.assertRaises(ValueError):
                ServiceabilityConfig.from_dict(bad)


class RealDataTests(unittest.TestCase):
    """Miraj -> Sangli example on the project's real dataset."""

    def _miraj_to_sangli_within_limits(self, policy: ServiceabilityPolicy
                                       ) -> tuple[str, str]:
        graph = policy.graph
        for r in sorted(graph.locations):
            if (graph.locations[r].kind != "Restaurant"
                    or policy.zone_of(r) != "Miraj"):
                continue
            for s in sorted(graph.locations):
                if (graph.locations[s].kind == "Society"
                        and policy.zone_of(s) == "Sangli"):
                    route = dijkstra(graph, r, s)
                    if (route.found and route.total_time_min <= 30
                            and route.total_distance_km <= 8):
                        return r, s
        self.fail("no short Miraj -> Sangli pair in the dataset")

    def test_miraj_restaurant_to_sangli_society_rejected(self) -> None:
        policy = ServiceabilityPolicy.default(_real_graph())
        restaurant, society = self._miraj_to_sangli_within_limits(policy)
        result = policy.check(restaurant, society)
        self.assertEqual(result.reason, Reason.OUTSIDE_SERVICE_ZONE)

    def test_miraj_to_sangli_accepted_when_explicitly_allowed(self) -> None:
        graph = _real_graph()
        base = ServiceabilityPolicy.default(graph)
        restaurant, society = self._miraj_to_sangli_within_limits(base)
        config = ServiceabilityConfig.from_json()
        config.allowed_pairs.add((restaurant, society))
        self.assertTrue(ServiceabilityPolicy(graph, config)
                        .is_serviceable(restaurant, society))

    def test_simulation_only_generates_serviceable_orders(self) -> None:
        sim = DeliverySimulation(_real_graph(), num_drivers=20,
                                 num_orders=50, seed=42)
        metrics = sim.run()
        for order in sim.orders:
            self.assertTrue(sim.serviceability.is_serviceable(
                order.restaurant_id, order.society_id))
        self.assertEqual(metrics.rejected_orders, 0)
        self.assertEqual(metrics.delivered_orders, 50)

    def test_reject_mode_keeps_unserviceable_orders_visible(self) -> None:
        graph = _real_graph()
        policy = ServiceabilityPolicy.default(graph)
        orders = generate_orders(graph, 100, seed=42, serviceability=policy,
                                 reject_unserviceable=True)
        # Same demand as generation without a policy (all pairs routable).
        plain = generate_orders(graph, 100, seed=42)
        self.assertEqual([(o.restaurant_id, o.society_id) for o in orders],
                         [(o.restaurant_id, o.society_id) for o in plain])
        rejected = [o for o in orders if o.status is OrderStatus.REJECTED]
        self.assertTrue(rejected)
        for order in orders:
            check = policy.check(order.restaurant_id, order.society_id)
            if check.serviceable:
                self.assertEqual(order.status, OrderStatus.PENDING)
            else:
                self.assertEqual(order.status, OrderStatus.REJECTED)
                self.assertEqual(order.rejection_reason, check.reason.value)
                self.assertTrue(order.rejection_message)

    def test_reject_mode_simulation(self) -> None:
        def run() -> DeliverySimulation:
            sim = DeliverySimulation(_real_graph(), num_drivers=50,
                                     num_orders=100, seed=42,
                                     reject_unserviceable=True)
            sim.run()
            return sim

        sim = run()
        m = sim.metrics
        self.assertEqual(m.total_orders, 100)
        self.assertGreater(m.rejected_orders, 0)
        self.assertEqual(m.delivered_orders + m.rejected_orders, 100)
        for order in sim.orders:
            if order.status is OrderStatus.REJECTED:
                self.assertIsNone(order.assigned_driver)
                self.assertFalse(order.pickup_route)
        again = run().metrics
        self.assertEqual(again.rejected_orders, m.rejected_orders)
        self.assertAlmostEqual(again.total_distance_km, m.total_distance_km,
                               places=6)


class DispatchIntegrationTests(unittest.TestCase):
    def test_generate_orders_respects_policy(self) -> None:
        policy = _policy()
        orders = generate_orders(policy.graph, 1, seed=3,
                                 serviceability=policy)
        self.assertEqual((orders[0].restaurant_id, orders[0].society_id),
                         ("RS1", "SS1"))  # the only serviceable pair

    def test_dispatch_rejects_unserviceable_order(self) -> None:
        policy = _policy()
        driver = Driver("D1", "SS1")
        order = Order("O0001", "RM1", "SS1")
        engine = DispatchEngine(policy.graph, algorithm="dijkstra",
                                serviceability=policy)

        delivered = engine.dispatch_order(order, [driver])

        self.assertFalse(delivered)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(order.rejection_reason, "OUTSIDE_SERVICE_ZONE")
        self.assertIsNone(order.assigned_driver)
        self.assertEqual(driver.status, DriverStatus.AVAILABLE)
        self.assertEqual(driver.current_node, "SS1")

    def test_dispatch_delivers_serviceable_order(self) -> None:
        policy = _policy()
        order = Order("O0001", "RS1", "SS1")
        engine = DispatchEngine(policy.graph, algorithm="dijkstra",
                                serviceability=policy)
        self.assertTrue(engine.dispatch_order(order, [Driver("D1", "SS1")]))
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_rejected_order_is_terminal(self) -> None:
        order = Order("O0001", "RM1", "SS1")
        order.transition(OrderStatus.REJECTED)
        with self.assertRaises(ValueError):
            order.transition(OrderStatus.ASSIGNED)


if __name__ == "__main__":
    unittest.main()
