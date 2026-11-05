"""Relay (hand-off) delivery simulation: drivers cooperating on one order.

Idea: on a long order the pickup driver (A) should not have to drive the
whole way if another driver (B) is *already going that way*. A carries the
food to a node H that B's planned route passes through and hands it over.
A is then free again early - near the restaurants, where the next orders
start. B finishes its own job (its customer waits only the hand-off
minutes) and then drives on to this order's customer.

Unlike ``src/simulation.py`` (which completes each order instantly), this is
a time-based simulation, because "a driver who passes the middle point at
the right moment" only makes sense when drivers take time to move:

* orders arrive over ``window_min`` minutes; food is ready
  ``FOOD_PREP_MIN`` after the order is placed;
* every driver works through its jobs in order (append-only schedule) and is
  busy from departure until its last job ends;
* travel times are the dataset's road times (fastest paths).

Two strategies run on exactly the same drivers and orders:

``direct``  one driver per order: the driver that delivers soonest
            (ties: least empty driving to the restaurant).
``relay``   same choice of A, but a long order may be handed to a driver B
            whose current job passes a node H. A relay is used only if

                net benefit = minutes driver A is freed
                            - B's extra work (hand-off + drive to customer)
                            - extra minutes for the customer
                            > 0

            A waits at most ``MAX_A_WAIT_MIN`` at H (B at most
            ``MAX_B_WAIT_MIN``), and the customer waits
            at most ``MAX_EXTRA_DELAY_MIN`` longer than with a direct
            delivery.
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

from src.cancellation import checkpoint
from src.driver import generate_drivers
from src.graph import METRIC_INDEX, Graph
from src.order import generate_orders
from src.serviceability import ServiceabilityPolicy

FOOD_PREP_MIN = 10.0         # food is ready this long after the order
HANDOFF_MIN = 2.0            # time to pass the food from A to B
RELAY_MIN_TRIP_MIN = 15.0    # only orders with a longer R -> S trip are split
MAX_A_WAIT_MIN = 10.0        # A waits at most this long at H for B
MAX_B_WAIT_MIN = 5.0         # B waits at most this long at H for A
MAX_EXTRA_DELAY_MIN = 10.0   # customer waits at most this much longer
STRATEGIES = ("direct", "relay")
CUSTOM_PREFIX = "MY"         # IDs of orders placed by the user: MY01, MY02, ...


# ---------------------------------------------------------------- routes
class RouteTable:
    """Fastest routes between nodes, computed on demand and cached.

    For every source asked about, one full Dijkstra run (the same algorithm
    as ``src/dijkstra.py``, just without stopping at a single target) gives
    the fastest time and the parent links to *every* node.
    """

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self._trees: dict[str, tuple[dict[str, float], dict[str, str]]] = {}

    def _tree(self, source: str) -> tuple[dict[str, float], dict[str, str]]:
        if source not in self._trees:
            w = METRIC_INDEX["time"]
            dist = {source: 0.0}
            parent: dict[str, str] = {}
            heap = [(0.0, source)]
            done: set[str] = set()
            while heap:
                cost, node = heapq.heappop(heap)
                if node in done:
                    continue
                done.add(node)
                for nbr, weights in self.graph.adjacency[node].items():
                    new = cost + weights[w]
                    if new < dist.get(nbr, math.inf):
                        dist[nbr] = new
                        parent[nbr] = node
                        heapq.heappush(heap, (new, nbr))
            self._trees[source] = (dist, parent)
        return self._trees[source]

    def time(self, u: str, v: str) -> float:
        return self._tree(u)[0].get(v, math.inf)

    def path(self, u: str, v: str) -> list[str]:
        dist, parent = self._tree(u)
        if v not in dist:
            return []
        path = [v]
        while path[-1] != u:
            path.append(parent[path[-1]])
        return path[::-1]


# ---------------------------------------------------------------- state
@dataclass
class Segment:
    """A driver driving one road ``a -> b`` between minutes ``t0`` and ``t1``."""

    t0: float
    t1: float
    a: str
    b: str
    order_ids: list[str]   # the order(s) this drive belongs to
    carrying: bool         # True when food is on board, False when empty


@dataclass
class FleetDriver:
    driver_id: str
    node: str                 # where the driver is when its schedule ends
    free_time: float = 0.0    # when its schedule ends
    segments: list[Segment] = field(default_factory=list)
    km: float = 0.0
    empty_km: float = 0.0
    busy_min: float = 0.0
    jobs: int = 0
    # Current (last) job: index of its first segment, and whether it ends
    # with this driver handing its food to someone else.
    job_start: int = 0
    job_ends_in_handoff: bool = False


@dataclass
class FleetOrder:
    order_id: str
    restaurant_id: str
    society_id: str
    created: float
    ready: float
    pickup_driver: str = ""
    delivery_driver: str = ""
    pickup_time: float = 0.0
    delivered_time: float = 0.0
    direct_delivered_time: float = 0.0   # what a direct delivery would give
    handoff_node: str | None = None
    handoff_time: float | None = None
    minutes_freed: float = 0.0           # driver A's time saved by the relay
    helper_extra_min: float = 0.0        # driver B's extra work

    @property
    def is_relay(self) -> bool:
        return self.handoff_node is not None

    @property
    def delivery_min(self) -> float:
        return self.delivered_time - self.created


@dataclass
class FleetMetrics:
    strategy: str
    orders: int
    relays: int
    avg_delivery_min: float
    p90_delivery_min: float
    max_delivery_min: float
    total_km: float
    empty_km: float
    driver_busy_min: float
    longest_drive_min: float      # longest single carry by one driver
    max_driver_km: float
    drivers_used: int


# ---------------------------------------------------------------- simulation
class RelaySimulation:
    """Runs one strategy ("direct" or "relay") on a fixed set of orders."""

    def __init__(self, graph: Graph, num_drivers: int = 25,
                 num_orders: int = 100, seed: int = 42,
                 window_min: float = 120.0,
                 routes: RouteTable | None = None,
                 custom_orders: tuple[tuple[str, str, float], ...] = (),
                 serviceability: ServiceabilityPolicy | None = None,
                 driver_nodes: tuple[str, ...] = ()
                 ) -> None:
        """``custom_orders``: extra (restaurant, society, minute) orders
        chosen by the user, added to the generated ones as MY01, MY02, ...
        ``driver_nodes``: start places chosen by the user; when given they
        replace the seeded drivers (D001, D002, ... in the order listed).
        ``num_orders`` may be 0 when custom orders are given."""
        if driver_nodes:
            num_drivers = len(driver_nodes)
        if num_drivers <= 0:
            raise ValueError("num_drivers must be positive.")
        if num_orders < 0 or num_orders + len(custom_orders) == 0:
            raise ValueError("Add at least one order.")
        for node in driver_nodes:
            if node not in graph.locations or not graph.is_connected_node(node):
                raise ValueError(f"{node} is not a place drivers can start from.")
        self.driver_nodes = tuple(driver_nodes)
        self.graph = graph
        self.num_drivers = num_drivers
        self.num_orders = num_orders
        self.seed = seed
        self.window_min = window_min
        self.routes = routes or RouteTable(graph)
        self.serviceability = serviceability
        for restaurant, society, minute in custom_orders:
            self._check_custom(restaurant, society, minute)
        self.custom_orders = tuple(custom_orders)

    def _check_custom(self, restaurant: str, society: str,
                      minute: float) -> None:
        locations = self.graph.locations
        if restaurant not in locations or                 locations[restaurant].kind != "Restaurant":
            raise ValueError(f"{restaurant} is not a restaurant.")
        if society not in locations or locations[society].kind != "Society":
            raise ValueError(f"{society} is not a housing society.")
        if not 0 <= minute <= self.window_min:
            raise ValueError(f"Order minute must be between 0 and "
                             f"{self.window_min:.0f}.")
        if self.serviceability is not None:
            result = self.serviceability.check(restaurant, society)
            if not result.serviceable:
                raise ValueError(result.message)
        elif math.isinf(self.routes.time(restaurant, society)):
            raise ValueError(f"No road route from {restaurant} to {society}.")

    # -- setup ---------------------------------------------------------
    def _make_drivers(self) -> list[FleetDriver]:
        if self.driver_nodes:
            return [FleetDriver(f"D{i:03d}", node)
                    for i, node in enumerate(self.driver_nodes, 1)]
        return [FleetDriver(d.driver_id, d.current_node)
                for d in generate_drivers(self.graph, self.num_drivers,
                                          self.seed)]

    def _make_orders(self) -> list[FleetOrder]:
        rng = random.Random(self.seed + 1)
        times = sorted(rng.uniform(0, self.window_min)
                       for _ in range(self.num_orders))
        pairs = (generate_orders(self.graph, self.num_orders, self.seed,
                                 serviceability=self.serviceability)
                 if self.num_orders else [])
        orders = [FleetOrder(o.order_id, o.restaurant_id, o.society_id,
                             created=round(t, 2),
                             ready=round(t + FOOD_PREP_MIN, 2))
                  for o, t in zip(pairs, times)]
        for i, (restaurant, society, minute) in enumerate(self.custom_orders, 1):
            orders.append(FleetOrder(f"{CUSTOM_PREFIX}{i:02d}", restaurant,
                                     society, created=round(minute, 2),
                                     ready=round(minute + FOOD_PREP_MIN, 2)))
        orders.sort(key=lambda o: o.created)   # dispatch in time order
        return orders

    # -- moving drivers -----------------------------------------------
    def _drive(self, driver: FleetDriver, target: str, depart: float,
               order_id: str, carrying: bool) -> float:
        """Append the fastest route to ``target``; return the arrival time."""
        t = depart
        path = self.routes.path(driver.node, target)
        for a, b in zip(path, path[1:]):
            km, minutes = self.graph.adjacency[a][b]
            driver.segments.append(Segment(round(t, 3), round(t + minutes, 3),
                                           a, b, [order_id], carrying))
            t += minutes
            driver.km += km
            if not carrying:
                driver.empty_km += km
        driver.node = target
        return t

    # -- planning ------------------------------------------------------
    def _best_direct(self, order: FleetOrder, drivers: list[FleetDriver]
                     ) -> tuple[FleetDriver, float, float]:
        """Driver giving the earliest delivery: (driver, pickup, delivered)."""
        trip = self.routes.time(order.restaurant_id, order.society_id)
        best = None
        for d in drivers:
            approach = self.routes.time(d.node, order.restaurant_id)
            start = max(d.free_time, order.created)
            pickup = max(start + approach, order.ready)
            key = (pickup + trip, approach, d.driver_id)
            if best is None or key < best[0]:
                best = (key, d, pickup)
        (delivered, _, _), driver, pickup = best
        return driver, pickup, delivered

    def _best_relay(self, order: FleetOrder, driver_a: FleetDriver,
                    pickup: float, direct_delivered: float,
                    drivers: list[FleetDriver]):
        """Best hand-off as (net, B, segment index, H, meet), or None if no
        driver going that way makes a relay worth it."""
        r, s = order.restaurant_id, order.society_id
        if self.routes.time(r, s) < RELAY_MIN_TRIP_MIN:
            return None
        best = None
        for b in drivers:
            # A driver whose job ends by handing food over cannot stop
            # earlier on that job without making the other driver wait.
            if b is driver_a or b.job_ends_in_handoff:
                continue
            # Preserve the timing of an already committed transfer.
            # Shifting this job again would leave its sender waiting alone.
            if any(len(seg.order_ids) > 1 for seg in b.segments[b.job_start:]):
                continue
            to_customer = self.routes.time(b.node, s)   # after B's own drop
            b_customers = len({o for seg in b.segments[b.job_start:]
                               for o in seg.order_ids})
            # Every node that B's current job still drives through.
            for i in range(b.job_start, len(b.segments)):
                h, passes = b.segments[i].a, b.segments[i].t0
                if h == r:      # B passes the restaurant itself: not a relay
                    continue
                a_at_h = pickup + self.routes.time(r, h)
                if not -MAX_B_WAIT_MIN <= passes - a_at_h <= MAX_A_WAIT_MIN:
                    continue
                meet = max(passes, a_at_h)
                stop = meet - passes + HANDOFF_MIN     # B's delay from here
                delivered = b.free_time + stop + to_customer
                extra = delivered - direct_delivered
                if extra > MAX_EXTRA_DELAY_MIN:
                    continue
                freed = direct_delivered - (meet + HANDOFF_MIN)
                # B's extra work, this customer's extra wait, and the delay
                # for B's own customers on this job.
                cost = (stop + to_customer + max(extra, 0.0)
                        + stop * b_customers)
                net = freed - cost
                if net > 0 and (best is None or net > best[0]):
                    best = (net, b, i, h, meet)
        return best

    def _hand_over(self, b: FleetDriver, index: int, meet: float,
                   order_id: str, orders: dict[str, FleetOrder]) -> None:
        """B stops at the start of segment ``index`` (waits for A until
        ``meet``, then HANDOFF_MIN). The rest of its job happens that much
        later and also carries ``order_id``."""
        shift_from = b.segments[index].t0
        shift = meet - shift_from + HANDOFF_MIN
        job_orders = {o for seg in b.segments[b.job_start:]
                      for o in seg.order_ids}
        for seg in b.segments[index:]:
            seg.t0 = round(seg.t0 + shift, 3)
            seg.t1 = round(seg.t1 + shift, 3)
            if not seg.carrying:
                b.empty_km -= self.graph.adjacency[seg.a][seg.b][0]
                seg.carrying = True
            seg.order_ids.append(order_id)
        b.free_time += shift
        b.busy_min += shift
        # B's own orders in this job: events after the stop shift too.
        for oid in job_orders:
            o = orders[oid]
            if o.pickup_driver == b.driver_id and o.pickup_time >= shift_from:
                o.pickup_time = round(o.pickup_time + shift, 2)
            if (o.delivery_driver == b.driver_id
                    and o.delivered_time >= shift_from):
                o.delivered_time = round(o.delivered_time + shift, 2)

    # -- run -----------------------------------------------------------
    def run(self, strategy: str = "relay"
            ) -> tuple[FleetMetrics, list[FleetDriver], list[FleetOrder]]:
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy must be one of {STRATEGIES}")
        drivers = self._make_drivers()
        orders = self._make_orders()
        by_id = {o.order_id: o for o in orders}

        for order in orders:
            checkpoint()
            a, pickup, direct_delivered = self._best_direct(order, drivers)
            order.direct_delivered_time = round(direct_delivered, 2)
            relay = (self._best_relay(order, a, pickup, direct_delivered,
                                      drivers)
                     if strategy == "relay" else None)

            a_start = max(a.free_time, order.created)
            a.job_start = len(a.segments)
            arrive = self._drive(a, order.restaurant_id, a_start,
                                 order.order_id, carrying=False)
            pickup = max(arrive, order.ready)
            order.pickup_driver = a.driver_id
            order.pickup_time = round(pickup, 2)
            a.jobs += 1

            if relay is None:
                done = self._drive(a, order.society_id, pickup,
                                   order.order_id, carrying=True)
                order.delivery_driver = a.driver_id
                order.delivered_time = round(done, 2)
                a.busy_min += done - a_start
                a.free_time = done
                a.job_ends_in_handoff = False
                continue

            _, b, index, h, meet = relay
            # Driver A: restaurant -> H, waits for B, hands over, is free.
            self._drive(a, h, pickup, order.order_id, carrying=True)
            handed = meet + HANDOFF_MIN
            a.busy_min += handed - a_start
            a.free_time = handed
            a.job_ends_in_handoff = True
            # Driver B: stops at H on its way, finishes its own job, then
            # drives on to this customer (all part of B's current job).
            b_start_shift = b.free_time
            self._hand_over(b, index, meet, order.order_id, by_id)
            b_end = b.free_time
            done = self._drive(b, order.society_id, b_end,
                               order.order_id, carrying=True)
            b.busy_min += done - b_end
            b.free_time = done

            order.delivery_driver = b.driver_id
            order.handoff_node = h
            order.handoff_time = round(meet, 2)
            order.delivered_time = round(done, 2)
            order.minutes_freed = round(direct_delivered - handed, 2)
            order.helper_extra_min = round(done - b_start_shift, 2)

        return self._metrics(strategy, drivers, orders), drivers, orders

    @staticmethod
    def _metrics(strategy: str, drivers: list[FleetDriver],
                 orders: list[FleetOrder]) -> FleetMetrics:
        times = sorted(o.delivery_min for o in orders)
        p90 = times[max(0, math.ceil(0.9 * len(times)) - 1)]
        # Longest stretch one driver spent carrying the same first order.
        longest = 0.0
        for d in drivers:
            run_start = None
            for i, seg in enumerate(d.segments):
                if not seg.carrying:
                    run_start = None
                    continue
                if run_start is None:
                    run_start = seg.t0
                nxt = d.segments[i + 1] if i + 1 < len(d.segments) else None
                if (nxt is None or not nxt.carrying or nxt.t0 > seg.t1 + 1e-6
                        or nxt.order_ids[0] != seg.order_ids[0]):
                    longest = max(longest, seg.t1 - run_start)
                    run_start = None
        return FleetMetrics(
            strategy=strategy,
            orders=len(orders),
            relays=sum(o.is_relay for o in orders),
            avg_delivery_min=sum(times) / len(times),
            p90_delivery_min=p90,
            max_delivery_min=times[-1],
            total_km=sum(d.km for d in drivers),
            empty_km=sum(d.empty_km for d in drivers),
            driver_busy_min=sum(d.busy_min for d in drivers),
            longest_drive_min=longest,
            max_driver_km=max(d.km for d in drivers),
            drivers_used=sum(1 for d in drivers if d.jobs),
        )


def compare_strategies(graph: Graph, num_drivers: int = 25,
                       num_orders: int = 100, seed: int = 42,
                       window_min: float = 120.0,
                       custom_orders: tuple[tuple[str, str, float], ...] = (),
                       serviceability: ServiceabilityPolicy | None = None,
                       driver_nodes: tuple[str, ...] = ()
                       ) -> dict[str, tuple]:
    """Run both strategies on the same drivers and orders."""
    routes = RouteTable(graph)
    sim = RelaySimulation(graph, num_drivers, num_orders, seed, window_min,
                          routes, custom_orders, serviceability, driver_nodes)
    return {name: sim.run(name) for name in STRATEGIES}
