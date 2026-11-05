"""Tests for the relay (hand-off) delivery simulation."""

from __future__ import annotations

import os
import unittest

from src.data_loader import load_dataset
from src.graph import build_graph
from src.relay import (FOOD_PREP_MIN, HANDOFF_MIN, RelaySimulation,
                       compare_strategies)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data")
EPS = 1e-6


class RelaySimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = build_graph(load_dataset(DATA_DIR))
        # Several runs, so the checks cover a good number of hand-offs.
        cls.runs = [compare_strategies(cls.graph, drivers, 100, seed)
                    for drivers, seed in ((25, 42), (20, 3), (35, 7))]

    def each_run(self):
        for result in self.runs:
            for strategy, (metrics, drivers, orders) in result.items():
                yield strategy, metrics, drivers, orders

    def test_direct_never_relays_and_relay_does(self) -> None:
        for result in self.runs:
            self.assertEqual(result["direct"][0].relays, 0)
        self.assertGreater(sum(r["relay"][0].relays for r in self.runs), 0)

    def test_every_order_delivered_after_food_is_ready(self) -> None:
        for _, metrics, _, orders in self.each_run():
            self.assertEqual(metrics.orders, 100)
            for o in orders:
                self.assertAlmostEqual(o.ready, o.created + FOOD_PREP_MIN,
                                       places=1)
                self.assertGreaterEqual(o.pickup_time, o.ready - EPS)
                self.assertGreater(o.delivered_time, o.pickup_time)

    def test_driver_never_in_two_places_at_once(self) -> None:
        for _, _, drivers, _ in self.each_run():
            for d in drivers:
                for prev, seg in zip(d.segments, d.segments[1:]):
                    self.assertEqual(prev.b, seg.a, d.driver_id)
                    self.assertGreaterEqual(seg.t0, prev.t1 - EPS, d.driver_id)

    def test_food_travels_restaurant_to_customer_without_gaps(self) -> None:
        for _, _, drivers, orders in self.each_run():
            for o in orders:
                legs = sorted(
                    ((seg.t0, seg.t1, seg.a, seg.b, d.driver_id)
                     for d in drivers for seg in d.segments
                     if o.order_id in seg.order_ids and seg.carrying),
                    key=lambda leg: leg[0])
                if not legs:   # restaurant is the customer's own node
                    continue
                self.assertEqual(legs[0][2], o.restaurant_id, o.order_id)
                self.assertEqual(legs[-1][3], o.society_id, o.order_id)
                for prev, leg in zip(legs, legs[1:]):
                    self.assertEqual(prev[3], leg[2], o.order_id)
                    self.assertGreaterEqual(leg[0], prev[1] - EPS, o.order_id)

    def test_handoff_is_between_two_drivers_who_meet(self) -> None:
        for strategy, _, drivers, orders in self.each_run():
            by_id = {d.driver_id: d for d in drivers}
            for o in (o for o in orders if o.is_relay):
                self.assertEqual(strategy, "relay")
                self.assertNotEqual(o.pickup_driver, o.delivery_driver)
                a, b = by_id[o.pickup_driver], by_id[o.delivery_driver]
                # A's last carry of this order arrives at H by the meeting.
                a_in = [s for s in a.segments
                        if o.order_id in s.order_ids and s.carrying]
                self.assertEqual(a_in[-1].b, o.handoff_node)
                self.assertLessEqual(a_in[-1].t1, o.handoff_time + EPS)
                # B leaves H with the food only after the hand-off.
                b_out = [s for s in b.segments
                         if o.order_id in s.order_ids and s.carrying]
                self.assertEqual(b_out[0].a, o.handoff_node)
                self.assertGreaterEqual(b_out[0].t0,
                                        o.handoff_time + HANDOFF_MIN - EPS)
                self.assertGreater(o.minutes_freed, 0)

    def test_fixed_seed_is_reproducible(self) -> None:
        first = RelaySimulation(self.graph, 25, 100, 42).run("relay")
        second = RelaySimulation(self.graph, 25, 100, 42).run("relay")
        self.assertEqual(first[0], second[0])
        self.assertEqual([o.delivered_time for o in first[2]],
                         [o.delivered_time for o in second[2]])

    def test_user_order_is_added_and_delivered(self) -> None:
        sim = RelaySimulation(self.graph, 25, 100, 42,
                              custom_orders=(("R115", "S019", 12.0),))
        for strategy in ("direct", "relay"):
            _, _, orders = sim.run(strategy)
            self.assertEqual(len(orders), 101)
            mine = [o for o in orders if o.order_id == "MY01"][0]
            self.assertEqual((mine.restaurant_id, mine.society_id),
                             ("R115", "S019"))
            self.assertEqual(mine.created, 12.0)
            self.assertGreater(mine.delivered_time, mine.pickup_time)
            # Orders are dispatched in the order they are placed.
            created = [o.created for o in orders]
            self.assertEqual(created, sorted(created))

    def test_user_order_must_be_restaurant_to_society(self) -> None:
        for bad in (("S001", "S019", 10), ("R115", "R001", 10),
                    ("R115", "S019", 500), ("R999", "S019", 10)):
            with self.assertRaises(ValueError):
                RelaySimulation(self.graph, 5, 5, 1, custom_orders=(bad,))

    def test_showcase_two_drivers_on_one_path_hand_off(self) -> None:
        orders = (("R017", "S002", 0.0), ("R170", "S002", 3.0),
                  ("R079", "S002", 6.0))
        sim = RelaySimulation(self.graph, 1, 0, 1, custom_orders=orders,
                              driver_nodes=("R017", "R170"))
        direct, relay = sim.run("direct"), sim.run("relay")
        self.assertEqual([d.driver_id for d in relay[1]], ["D001", "D002"])
        self.assertEqual(direct[0].relays, 0)
        self.assertGreaterEqual(relay[0].relays, 1)
        handed = [o for o in relay[2] if o.is_relay]
        self.assertNotEqual(handed[0].pickup_driver, handed[0].delivery_driver)
        self.assertLess(relay[0].avg_delivery_min, direct[0].avg_delivery_min)

    def test_two_order_showcase_sender_stays_at_handoff(self) -> None:
        sim = RelaySimulation(
            self.graph, 2, 0, 3,
            custom_orders=(("R017", "S002", 0), ("R170", "S002", 3)),
            driver_nodes=("R017", "R170"))
        _, drivers, orders = sim.run("relay")
        handed = next(o for o in orders if o.is_relay)
        sender = next(d for d in drivers if d.driver_id == handed.pickup_driver)
        receiver = next(d for d in drivers if d.driver_id == handed.delivery_driver)
        self.assertEqual(sender.node, handed.handoff_node)
        self.assertEqual(sender.segments[-1].b, handed.handoff_node)
        self.assertFalse(any(s.t0 >= handed.handoff_time + HANDOFF_MIN
                             for s in sender.segments))
        self.assertEqual(receiver.node, handed.society_id)
        self.assertTrue(any(s.t0 >= handed.handoff_time + HANDOFF_MIN
                            for s in receiver.segments))

    def test_driver_nodes_and_empty_order_validation(self) -> None:
        with self.assertRaises(ValueError):
            RelaySimulation(self.graph, 2, 0, 1)
        with self.assertRaises(ValueError):
            RelaySimulation(self.graph, 2, 3, 1, driver_nodes=("NOPE",))

    def test_rejects_unknown_strategy(self) -> None:
        with self.assertRaises(ValueError):
            RelaySimulation(self.graph, 5, 5, 1).run("teleport")


if __name__ == "__main__":
    unittest.main()
