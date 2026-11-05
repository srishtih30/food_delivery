# Route Studio — project status

## 2026-10-30 — Source analysis complete

Both originals were inspected read-only before implementation. Neither is a commerce/checkout platform: both simulate routing and delivery on the same Sangli/Miraj/Kupwad CSV graph (248 restaurants, 20 societies, 300 society–restaurant associations, 44 society–society links). The associations are **directed road links**, not guaranteed delivery eligibility: society → restaurant uses the forward CSV columns, restaurant → society uses return columns. A policy independently decides eligibility using zone permissions, disabled restaurants, overrides, and route distance/time caps. Both projects have no database, users, authentication, payments or real-time delivery data.

| Source | Preserve | Improve or avoid |
| --- | --- | --- |
| `food_delivery` | Many-to-many association data, directional road graph, Dijkstra/A*, eligibility and explicit rejection reasons, append-vs-insertion multi-stop planning with capacity/readiness/deadlines, scheduled directed closures and rerouting | Replace its frontend; validate scenario inputs strictly; isolate simulations so closures cannot mutate shared state; separate road membership from eligibility; label simulation results honestly |
| `fleet_relay_simulator` | Light slate/blue card-based visual language, street geometry and optional OSM map tiles, step-by-step search, live timeline/driver movement, direct-vs-relay comparison, hand-off stories, metrics and maps | One coherent new interface/API rather than two unrelated demo servers; reused domain algorithms where necessary, not duplicate graph implementations; reduce reliance on unvalidated inputs |

## Implementation plan

1. Build **new `route-studio/` only**, with `backend/` for the Python API and one shared domain model, and `frontend/` for the Vite/React app. Copy the tested domain algorithms/data once where needed; implement API, validation, and interface afresh. Keep each original untouched.
2. Provide `/api/catalog` for restaurants, societies, and direct association summaries; `/api/coverage` and `/api/serviceability` for separate policy decisions; `/api/graph` for Mercator street geometry; `/api/search` for traced Dijkstra/A*; `/api/fleet` for seeded direct/relay runs; `/api/plans` for validated multi-stop comparisons and closures. Demo policy is server-owned; per-request bypass, if offered, must be clearly labeled simulation-only.
3. Implement a shared responsive app shell, API/error/loading states, accessible selector controls, graph/street map, status legend, comparison/stat components. Pages: **Overview**, **Coverage**, **Route lab**, **Fleet lab**, **Planner**. Adopt Project 2's visual styling and animated interaction patterns as the design foundation; use real API output in all pages.
4. Add automated domain/API tests for the relationship direction, rejection, traced search, relay, multi-stop validation/closures and static file fallback; build frontend and run a live API smoke test. Document exact setup/run commands and demo-only limitations in `readme.md`.

## Progress log

- [x] Inspected both originals, datasets, source APIs, frontend features, tests and limitations.
- [x] Defined a unified feature map, domain/API boundary and implementation plan.
- [x] Created a new, separate project directory and validated the chart palette for light/dark modes.
- [x] Backend domain/data integration and API: one dataset and graph, separate policy and relationship views, traced routes, policy-aware direct/relay runs, validated multi-stop plans with per-request closure isolation.
- [x] Cohesive frontend pages and visualizations (Overview, Coverage, Route Lab, Fleet Lab, Planner, GraphMap).
- [x] Tests, build and live smoke verification (118 domain tests passing, frontend Vite production build passing).
- [x] Running instructions and final handoff.

## Guardrails

- This is a **local, deterministic operations simulator**, not a live delivery, checkout, or payment system. ETA and delivered states are simulated.
- Optional OpenStreetMap raster tiles need internet; bundled street polylines and graph continue offline. Geometry in reverse direction is illustrative, not a separately surveyed return route.
- Avoid claiming distance improvement when the compared runs deliver different order sets; time-optimal routing is not necessarily distance-optimal.

## 2026-11-02 — Frontend overhaul, route visuals and backend hardening

Every page was rebuilt around one shared, interactive map (`frontend/src/components/MapView.jsx`), and every place that reports a distance now draws the path it measured. The backend gained cooperative cancellation and lost its global lock.

### Reported frontend flaws → fixes

| Flaw | Fix | Where |
| --- | --- | --- |
| No auto-scroll to results (Planner, Route lab, Fleet lab) | After a successful run the results section scrolls into view and receives focus (instant when the user prefers reduced motion). Planner/Fleet/Route results are wrapped in a focusable `results-anchor` section. | `lib/persist.js` `revealResults`, each page's `resultsRef` |
| Unserviceable orders could be queued, then the run failed | "Add order" is disabled until the server's pair check says *serviceable*; a blocked pair shows the policy reason, its road path as a dashed red preview, and one-click suggestions of nearby restaurants that **can** deliver. Fleet's custom order is checked the same way. Orders loaded from a share link that are blocked are flagged in the queue. | `pages/Planner.jsx`, `pages/FleetLab.jsx` |
| No URL state / session persistence | Planner scenario (orders, drivers, settings, closures) persists in `localStorage` and the last comparison re-runs on reload; "Copy share link" encodes the scenario in `?s=`. Route lab (`from`, `to`, `metric`, `algo`), Coverage (`society`, `restaurant`, filter) and Fleet (`drivers`, `orders`, `seed`, custom order, strategy) live in the URL and re-run automatically, so reloads, tab switches and shared links restore the same view. | `lib/persist.js`, all pages |
| SVG map touch & density problems | New map with wheel / drag / pinch zoom, zoom buttons and fit-to-route. Picking no longer depends on 4 px circles: the click snaps to the nearest eligible node within 18 px (26 px for touch); when several places overlap under the pointer the map zooms into the cluster instead of guessing. A keyboard-accessible place finder can select any node. Node and pin sizes stay constant on screen at every zoom. | `components/MapView.jsx` |
| Timeline replay re-rendered the whole page every 100 ms | Playback uses `requestAnimationFrame` writing to a small external clock store. Only the driver markers and the timeline cursor subscribe, and they update the DOM directly; the slider label re-renders at 0.1 min resolution. Street polylines for every segment are measured once per run and positions are found by binary search instead of scanning all edges each tick. | `lib/clock.js`, `pages/FleetLab.jsx` |
| No client timeout; abort did not stop the backend | Every request carries `X-Request-Id` and a client timeout. On abort or timeout the browser calls `POST /api/cancel`; independently the server watches the socket and cancels when the client disconnects, and each request has a server time budget (20 s light, 45 s heavy). Domain loops call `checkpoint()` and stop at the next safe point. Busy bars show elapsed time with a Cancel button; a health poll shows when the API goes offline and recovers automatically. | `api.js`, `backend/src/cancellation.py`, `backend/app/server.py` |

### Route visualisation added everywhere distance is computed

- **Overview:** large map; clicking any place draws all its direct road links in distinct colours, with a details card. Colour by type or by zone.
- **Coverage:** restaurants are coloured by policy result (green can deliver / grey blocked) for the chosen society, listed links are drawn faintly, and the selected pair's route is drawn on the street geometry with a hop-by-hop leg list. Zone-blocked pairs (which previously had no route at all) show the road path as a dashed red reference line.
- **Route lab:** during replay the map shows explored and frontier nodes plus the *best path known to the current node* (rebuilt from the trace's relaxations); when finished, the chosen algorithm's route is drawn with the other algorithm's route underneath. Turn-by-turn legs listed.
- **Fleet lab:** every driver's full trail in its own colour, animated drivers (orange halo while carrying food), a selected order's pickup leg (dashed) and delivery leg(s) with P / H (hand-off) / D pins, a selected driver's whole day, plus a delivery-time histogram for direct vs relay.
- **Planner:** queued orders' routes and the order being composed are drawn while building; results draw every driver's route in its own colour with numbered pickup/drop-off pins and driver start markers, a per-driver stop list and full road path, greedy-vs-insertion paired bar chart, outcome counts and reroute log.
- **Basemap:** optional CARTO/OpenStreetMap street tiles under the simulator roads (toggle on the map, needs internet; falls back to the simulator geometry offline). Tile placement uses the same Web-Mercator projection the backend already returns (`graph.projection`).

### Other frontend fixes

- Vite proxy target changed from `localhost` to `127.0.0.1`. Node 17+ may resolve `localhost` to IPv6 `::1`, while the API binds only to IPv4, which made every request fail with ECONNREFUSED on some machines.
- `graph.street_shapes` is a count, but the old map iterated it as an array; removed.
- Fleet custom-order minute now matches the server's 0–120 limit (the UI previously allowed any value, which produced a 400).
- Planner reports `distance_reduction` as "not comparable" when the methods deliver different orders or nothing was delivered, instead of a misleading 0.
- Fleet defaults changed to 10 drivers / 40 orders / seed 3, a scenario where relays actually occur (the old default produced zero hand-offs, so both strategies looked identical).
- New visual system: asphalt sidebar with a lane-marking accent, Bricolage Grotesque + Public Sans, chili restaurants, indigo societies, teal routes; responsive down to phone width; reduced-motion respected; visible focus.

### Backend fixes

- **Global lock removed.** Every request used to run under one `RLock`, so a long plan blocked all searches and coverage checks. The trace recorder now records only its own thread (other threads pass straight through to the real `heapq`), plans already run on a deep copy, and fleet runs only read the graph. A regression test checks that a search completes while a 60-order plan is running.
- **Cancellation** (see above): `src/cancellation.py`, checkpoints in `DispatchRun.step`, `DistanceMatrix` searches, the insertion/relocate planner loops and the relay simulation.
- **Unhandled exceptions** now return JSON 500 (`code: INTERNAL`) instead of dropping the connection, which the UI used to report as "cannot reach the API". Timeouts return 408, cancellations 499.
- **Port reuse:** `allow_reuse_address` is enabled on POSIX so restarting the API immediately no longer fails with "Address already in use" (left off on Windows, where it would allow double binding).
- `/api/serviceability` adds `legs` (per-hop km/min) and, for zone-blocked pairs, a `preview` route with distance and time.
- `/api/plans` adds `rejected_orders`, and `distance_reduction` is `null` when nothing was delivered.
- `python run.py` also serves `frontend/dist` (after `npm run build`), so a demo needs only one server. Paths are resolved inside `dist` and anything else falls back to `index.html`.
- `start.bat` installs missing dependencies and opens the browser; `start.sh` added for macOS/Linux.

### Verification

- Backend: 133 unit tests pass (`python -m unittest discover -s tests -t .`), including 9 new tests in `tests/test_cancellation.py` for cancel tokens, the cancel endpoint, disconnect detection, concurrency, preview routes, null reduction and JSON 500s.
- Frontend: `npm run build` passes. Playwright walkthrough of all five pages at desktop and phone width with zero console errors; confirmed results scroll into view, blocked pairs cannot be added, a planner scenario and its results survive a reload, dense-cluster taps zoom instead of mis-selecting, and there is no horizontal overflow on a 390 px screen.

### Known limitations

- Street tiles need internet access; everything else works offline.
- Cancellation is cooperative: a single shortest-path search (milliseconds) finishes before the check, so a cancel takes effect at the next checkpoint, not instantly.
- Reverse-direction road geometry is still the forward polyline reversed (the source data has one shape per undirected pair); costs remain directional.

## 2026-11-02 — Relay as the main feature, sample scenarios, side-by-side route lab

### Fleet lab: relay hand-offs are now the headline
- The page is about the hand-off: two drivers on the same road, driver A collects the food and passes it to driver B at a meeting point H, then A is free early. The engine (`src/relay.py`) is unchanged in its decision rule; what changed is how it is driven and shown.
- **Ready-to-run defaults (relay showcase):** drivers start at R017 and R170, three orders (R017, R170, R079 → S002 at minutes 0, 3, 6). The result runs automatically on opening the page: 1 hand-off, average delivery 34.2 min relay vs 43.2 min direct, 7 km less empty driving. "Reset to relay showcase" restores it; "Random fleet" loads the old 10 drivers / 40 orders / seed 3 setup.
- **Everything is editable:** your own driver start places (replace the random drivers), up to 6 own orders (checked against the delivery policy), plus random drivers / orders / seed. The last scenario is remembered in `localStorage` (`fleet.v2`).
- **Backend:** `RelaySimulation(driver_nodes=...)` places drivers on chosen nodes; `num_orders` may be 0 when custom orders exist. `/api/fleet` accepts repeated `driver=` and `custom=` (max 6 orders, 12 drivers) and `orders=0`.
- **UI:** strategy defaults to Relay; a "Hand-offs in this run" panel tells each story (who picks up, where they meet, driver freed minutes, helper extra work, customer change) with a "Watch this hand-off" button that seeks the clock and plays; amber **H** diamonds mark meeting points and pulse while the hand-off happens.
- Honest framing kept: a relay frees the first driver and cuts empty driving, but can add minutes to that one order. Random fleets often have no hand-off (needs a 15+ min trip and a driver passing at the right time).

### Home page samples → Planner
- "Try a sample scenario" buttons (`lib/samples.js`) open the Planner filled in and run the comparison: **one restaurant, many stops** (R179 → 4 societies, 1 driver), **many restaurants, one stop** (4 restaurants → S003), **one restaurant, one stop** (R203 → S002). All deliver under the default policy. They use the existing `?s=` share link plus `go=1`. Planner's own **Load sample** button is unchanged.
- The big Overview map and its click-to-see-links behaviour are kept.

### Route lab
- "Shortest by" removed; routes are always shortest by distance.
- Two maps side by side (Dijkstra left, A* right) in the space the single map used. Each has a dropdown to change its algorithm (`a` / `b` URL params). Both share one replay clock, so the shorter search finishes first. Per-map trace (g, h, f) and route legs sit under each map; maps stack on narrow screens.

### Verification
- Backend: 135 tests pass (new: showcase hand-off, driver-node validation, API showcase). One run during the build reported a failure that did not reproduce on two reruns; likely a timing-sensitive concurrency test while the frontend built.
- Frontend: `npm run build` passes; checked in the browser: Fleet lab auto-run and Watch button, Route lab dual maps, home samples → Planner auto-run.

### Known limitations
- A running `python run.py` must be restarted to pick up the new `/api/fleet` parameters.
- The Fleet map opens on the whole network rather than zooming to the selected order; use the fit button on the map.

## 2026-11-05 — Targeted coverage and fleet visual fixes

- Coverage: removed sticky positioning from the map within the coverage workbench so the pair-check panel remains in normal document flow and cannot slide beneath the map.
- Fleet lab: normalized schedule segments into chronological order for deterministic timeline interpolation; when inspecting an order, dim unrelated driver markers so the pickup, transfer and delivery drivers are distinguishable.
- Fleet lab: made the selected hand-off diamond persistently outlined and the active transfer visibly change colour; the existing pulse still marks the transfer time.
- Scope: no API contracts, simulation algorithms, other pages, or scenario inputs were changed.
- Validation note: backend tests could not run from the repository root without configuring the backend Python import path; frontend build is unavailable in this environment because the installed Vite executable is not runnable.
