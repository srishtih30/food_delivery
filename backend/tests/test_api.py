"""HTTP contracts and integration regressions for the unified application."""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.server import create_server
from app.services import StudioService


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = StudioService()
        cls.server = create_server(port=0, service=cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        request = Request(self.base + path, data=body,
                          headers={"Content-Type": "application/json"} if body else {})
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_catalog_is_many_to_many_and_directional(self):
        status, catalog = self.request("/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["summary"], {"restaurants": 248, "societies": 20,
                                             "relationships": 300, "roads": 688})
        self.assertTrue(any(r["linked_societies"] > 1 for r in catalog["restaurants"]))
        self.assertTrue(all(s["linked_restaurants"] > 1 for s in catalog["societies"]))
        link = next(x for x in catalog["links"] if x["restaurant_id"] == "R187" and x["society_id"] == "S001")
        self.assertNotEqual(link["forward_km"], link["return_km"])
        self.assertEqual(self.service.graph.edge("S001", "R187")[0], link["forward_km"])
        self.assertEqual(self.service.graph.edge("R187", "S001")[0], link["return_km"])

    def test_coverage_and_policy_are_distinct_from_links(self):
        status, coverage = self.request("/api/coverage?society=S001")
        self.assertEqual(status, 200)
        self.assertEqual(coverage["counts"]["linked"], 15)
        eligible_indirect = next(r for r in coverage["restaurants"] if r["serviceable"] and not r["linked"])
        _, check = self.request("/api/serviceability?" + urlencode({"restaurant": eligible_indirect["id"], "society": "S001"}))
        self.assertTrue(check["serviceable"])
        self.assertTrue(check["route"])
        _, invalid = self.request("/api/serviceability?restaurant=BOGUS&society=S001")
        self.assertFalse(invalid["serviceable"])
        self.assertEqual(invalid["reason"], "INVALID_RESTAURANT_OR_SOCIETY")

    def test_graph_and_traced_search(self):
        status, graph = self.request("/api/graph")
        self.assertEqual(status, 200)
        self.assertEqual(graph["stats"]["locations"], 268)
        self.assertGreater(graph["street_shapes"], 0)
        status, result = self.request("/api/search?source=R187&target=S001&metric=time")
        self.assertEqual(status, 200)
        self.assertTrue(result["dijkstra"]["verified"] and result["astar"]["verified"])
        self.assertEqual(result["dijkstra"]["cost"], result["astar"]["cost"])
        self.assertEqual(self.request("/api/search?source=R187&target=S001&metric=bad")[0], 400)
        self.assertEqual(self.request("/api/search?source=R187&target=R187")[0], 400)

    def test_fleet_compares_same_policy_eligible_orders(self):
        status, result = self.request("/api/fleet?drivers=8&orders=8&seed=42&custom=R187,S001,12")
        self.assertEqual(status, 200)
        self.assertEqual([o["id"] for o in result["direct"]["orders"]],
                         [o["id"] for o in result["relay"]["orders"]])
        self.assertIn("MY01", result["custom"])
        for order in result["relay"]["orders"]:
            self.assertTrue(self.service.policy.check(order["r"], order["s"]).serviceable)
        status, showcase = self.request("/api/fleet?orders=0&driver=R017&driver=R170&custom=R017,S002,0&custom=R170,S002,3&custom=R079,S002,6")
        self.assertEqual(status, 200)
        self.assertEqual(showcase["drivers"], 2)
        self.assertGreaterEqual(showcase["relay"]["metrics"]["relays"], 1)
        self.assertEqual(self.request("/api/fleet?orders=0")[0], 400)
        self.assertEqual(self.request("/api/fleet?drivers=999&orders=8")[0], 400)
        self.assertEqual(self.request("/api/fleet?drivers=8&orders=8&custom=R187,S001,nan")[0], 400)

    def test_planner_closures_are_isolated_and_input_validation(self):
        initial = self.service.graph.edge("S001", "R187")
        payload = {"orders": [{"restaurant": "R187", "society": "S001"}],
                   "drivers": ["S001"], "capacity": 2,
                   "closures": [{"source": "S001", "target": "R187", "at": 0, "reopen_at": 5}]}
        status, report = self.request("/api/plans", payload)
        self.assertEqual(status, 200)
        self.assertIn("append", report["methods"])
        self.assertIn("insertion", report["methods"])
        self.assertEqual(self.service.graph.edge("S001", "R187"), initial)
        self.assertEqual(self.service.graph.closed_roads, {})
        for invalid in (
            {**payload, "orders": [1]},
            {**payload, "capacity": True},
            {**payload, "closures": [{"source": "S001", "target": "R187", "at": -1}]},
            {**payload, "closures": [{"source": "S001", "target": "R187", "at": float("nan")} ]},
            {**payload, "enforce_serviceability": False},
        ):
            self.assertEqual(self.request("/api/plans", invalid)[0], 400)

    def test_errors_are_json(self):
        status, error = self.request("/api/unknown")
        self.assertEqual(status, 404)
        self.assertIn("error", error)
        self.assertEqual(self.request("/api/coverage?society=R187")[0], 400)


if __name__ == "__main__":
    unittest.main()
