"""Cancellation, concurrency and error-contract regressions for the API."""

import json
import socket
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.server import create_server
from app.services import StudioService
from src.cancellation import CancelToken, Cancelled, checkpoint, use_token
from src.scenario import ScenarioSpec, run_scenario
from src.serviceability import ServiceabilityPolicy


def big_plan(service, orders=60, drivers=30):
    cat = service.catalog_payload
    rs = [r["id"] for r in cat["restaurants"] if r["zone"] in ("Sangli", "Kupwad")]
    ss = [s["id"] for s in cat["societies"] if s["zone"] in ("Sangli", "Kupwad")]
    return {"orders": [{"restaurant": rs[(i * 7) % len(rs)], "society": ss[(i * 3) % len(ss)]}
                       for i in range(orders)],
            "drivers": (ss + rs)[:drivers], "capacity": 3,
            "algorithm": "dijkstra", "improve": "relocate"}


class CancelTokenTests(unittest.TestCase):
    def test_checkpoint_is_noop_without_token(self):
        checkpoint()  # must not raise outside a request

    def test_cancelled_token_stops_domain_run(self):
        service = StudioService()
        token = CancelToken()
        token.cancel("cancelled by client")
        spec = ScenarioSpec.from_dict(big_plan(service, 10, 5))
        with use_token(token), self.assertRaises(Cancelled):
            run_scenario(service.graph, spec, ServiceabilityPolicy(service.graph, service.policy.config))

    def test_budget_expiry_cancels(self):
        token = CancelToken(budget_s=0.001)
        time.sleep(0.01)
        self.assertTrue(token.cancelled)
        self.assertEqual(token.reason, "timeout")


class CancellableApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = StudioService()
        cls.server = create_server(port=0, service=cls.service, serve_frontend=False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, data=None, headers=None):
        body = None if data is None else json.dumps(data).encode()
        hdrs = {"Content-Type": "application/json"} if body else {}
        hdrs.update(headers or {})
        try:
            response = urlopen(Request(self.base + path, data=body, headers=hdrs), timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_cancel_endpoint_validates_and_reports_unknown(self):
        self.assertEqual(self.request("/api/cancel", {"id": "../x"})[0], 400)
        status, body = self.request("/api/cancel", {"id": "not-running"})
        self.assertEqual(status, 200)
        self.assertFalse(body["cancelled"])

    def test_disconnect_stops_server_work(self):
        payload = json.dumps(big_plan(self.service)).encode()
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        sock.sendall(b"POST /api/plans HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(payload)}\r\nX-Request-Id: drop-me\r\n\r\n".encode() + payload)
        deadline = time.time() + 5
        while len(self.server.registry) == 0 and time.time() < deadline:
            time.sleep(0.005)
        sock.close()  # browser abort
        deadline = time.time() + 5
        while len(self.server.registry) and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.server.registry), 0, "worker kept running after disconnect")

    def test_light_requests_are_not_blocked_by_heavy_plan(self):
        results = {}

        def heavy():
            results["plan"] = self.request("/api/plans", big_plan(self.service))[0]

        worker = threading.Thread(target=heavy)
        worker.start()
        time.sleep(0.05)
        started = time.perf_counter()
        status, _ = self.request("/api/search?source=R187&target=S001&metric=time")
        light = time.perf_counter() - started
        worker.join()
        self.assertEqual(status, 200)
        self.assertEqual(results["plan"], 200)
        self.assertLess(light, 2.0)

    def test_rejected_pair_has_preview_route(self):
        status, body = self.request("/api/serviceability?restaurant=R001&society=S001")
        self.assertEqual(status, 200)
        self.assertFalse(body["serviceable"])
        self.assertEqual(body["route"], [])
        self.assertEqual(body["preview"]["route"][0], "R001")
        self.assertEqual(body["preview"]["route"][-1], "S001")
        self.assertAlmostEqual(sum(leg["km"] for leg in body["preview"]["legs"]),
                               body["preview"]["distance_km"], places=2)
        _, ok = self.request("/api/serviceability?restaurant=R187&society=S001")
        self.assertTrue(ok["serviceable"])
        self.assertIsNone(ok["preview"])
        self.assertEqual(len(ok["legs"]), len(ok["route"]) - 1)

    def test_all_rejected_plan_has_no_reduction(self):
        status, body = self.request("/api/plans", {"orders": [{"restaurant": "R001", "society": "S001"}],
                                                   "drivers": ["S001"]})
        self.assertEqual(status, 200)
        self.assertIsNone(body["distance_reduction"])
        self.assertEqual(body["rejected_orders"][0]["reason"], "OUTSIDE_SERVICE_ZONE")

    def test_unexpected_errors_are_json_500(self):
        original = self.service.coverage
        self.service.coverage = lambda society: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            status, body = self.request("/api/coverage?society=S001")
        finally:
            self.service.coverage = original
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "INTERNAL")


if __name__ == "__main__":
    unittest.main()
