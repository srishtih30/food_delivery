# Route Studio

A local delivery-operations simulator combining the many-to-many delivery network, eligibility rules and multi-stop planner from `food_delivery` with the route-search and fleet-relay simulations and visual language of `fleet_relay_simulator`.

**This is a simulation, not a real delivery or ordering platform.** Orders, driver positions, service zones, delivery outcomes and ETAs are modeled. There are no accounts, checkout, payments, real-time traffic, live driver locations, or persistent order records. The bundled zone policy is synthetic and must not be interpreted as actual commercial coverage.

## Requirements

- Python 3.10+ with pip (3.13 tested)
- Node.js 20.19+ or 22.12+ with npm
- A modern web browser

The app runs entirely on your computer. The optional street basemap (CARTO / OpenStreetMap tiles, toggled with the layers button on any map) needs an internet connection; the bundled road geometry and the rest of the simulator work offline after dependencies are installed.

## Run locally

**Quick start:** on Windows double-click `start.bat`; on macOS/Linux run `./start.sh`. Both install missing dependencies, start the API and the UI, and (on Windows) open the browser. No environment variables or API keys are needed.

**Single server:** after `npm run build` in `frontend/`, `python run.py` also serves the built app at http://127.0.0.1:8060, so only one terminal is needed for a demo.

Or run the two servers by hand, in **two terminals** from the project directory:

### 1. Start the backend

On Windows, in PowerShell:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

If PowerShell's execution policy prevents activation, call `.\.venv\Scripts\python.exe` directly instead. In Git Bash on Windows use `source .venv/Scripts/activate`; on macOS/Linux use `python3 -m venv .venv` and `source .venv/bin/activate`.

The API listens only on **http://127.0.0.1:8060**. Check `http://127.0.0.1:8060/api/health` for `{"status":"ok"}`. The port can be changed with `python run.py --port 8061`; if you change it, also change the Vite proxy target in `frontend/vite.config.js`.

### 2. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite, usually **http://localhost:5173**. Vite proxies `/api` requests to the backend, so both servers must be running. Stop either server with Ctrl+C.

### Production frontend build

```bash
cd frontend
npm run build
npm run preview
```

The Vite preview server also proxies `/api` to the local backend. This is a local demonstration setup, not a hardened public deployment.

## What you can do

Every map supports wheel/drag/pinch zoom, a place finder, fit-to-route and an optional street basemap. Wherever a distance is computed, its road path is drawn.

- **Overview:** the whole network on a large map; click any place to draw its direct road links.
- **Coverage:** pick a society (list or map) to colour every restaurant by policy result, then pick a restaurant to draw its delivery route with a hop-by-hop leg list. Zone-blocked pairs show their road path as a dashed reference line.
- **Route lab:** compare Dijkstra and A*; replay the server trace with explored/frontier nodes and the best path known so far, then both final routes overlaid. Click the map to set A and B.
- **Fleet lab:** run seeded orders and drivers under direct and relay strategies; watch drivers move along real streets, follow one order (pickup, hand-off, drop-off) or one driver, scrub the timeline, compare metrics and the delivery-time histogram.
- **Planner:** queue orders (blocked pairs cannot be added; alternatives are suggested), drivers and directed road closures, then compare greedy append with cheapest insertion. Each driver's route is drawn with numbered stops. Use **Load sample** for a ready scenario and **Copy share link** to share one.

State is kept across reloads: the planner scenario in browser storage, the other pages in the URL (so links are shareable). Long requests show elapsed time and a Cancel button; cancelling or timing out also stops the computation on the server.

Direct road relationships are **many-to-many**, but they are not a list of authorized deliveries. The data supplies distinct society → restaurant and restaurant → society costs. The policy separately checks zone rules, explicit pair exceptions, route existence, and distance/time limits on the selected fastest route. An indirect route can be serviceable even without a direct association. The policy file is `backend/config/serviceability.json`; restart the API after changing it.

## Verify

From `backend/` with its environment active:

```bash
python -m unittest discover -s tests -t . -v
```

From `frontend/`:

```bash
npm run build
```

## API

All responses are JSON. Invalid input returns a JSON `error` with HTTP 400 (or 413/415 for malformed body size/content type).

| Method and path | Purpose |
| --- | --- |
| `GET /api/health` | Health check |
| `GET /api/catalog` | Locations, many-to-many link rows, dataset summary and synthetic policy |
| `GET /api/graph` | Directed weights, projected nodes and bundled road shapes |
| `GET /api/coverage?society=S001` | All restaurants with independent policy and direct-link status |
| `GET /api/serviceability?restaurant=R187&society=S001` | Pair eligibility, reason and selected route |
| `GET /api/search?source=R187&target=S001&metric=time` | Dijkstra and A* traces (`metric=time` or `distance`) |
| `GET /api/fleet?drivers=8&orders=8&seed=42&custom=R187,S001,12` | Direct vs relay simulation; `custom` is optional and may be repeated up to three times |
| `POST /api/plans` | Multi-stop plan comparison; accepts orders, driver start IDs, capacity, objective and closure events |
| `POST /api/cancel` | `{"id": "<X-Request-Id>"}` stops that request's computation |

Send an `X-Request-Id` header (1–64 letters, digits, `-` or `_`) to make a request cancellable by `/api/cancel`. The server also cancels a computation when the client disconnects, and enforces a time budget (20 s for most endpoints, 45 s for `/api/plans` and `/api/fleet`). Status codes: 400 invalid input, 408 time budget exceeded, 499 cancelled, 500 internal error (always JSON with `error`).

`/api/serviceability` returns `route` and per-hop `legs`; when a pair is rejected before routing (zone rule, deny list) it also returns a `preview` with the road path, distance and time for display only.

Example planner request:

```json
{
  "orders": [{"restaurant":"R187", "society":"S001", "ready_at":10, "deadline":60}],
  "drivers": ["S001", "R187"],
  "capacity": 2,
  "algorithm": "tree",
  "metric": "time",
  "improve": "relocate",
  "max_ride_extra_min": 20,
  "closures": [{"source":"S001", "target":"R187", "at":0, "reopen_at":5}]
}
```

A closure affects only the direction shown; runs operate on a separate graph copy. Input bounds are enforced to keep a local demo responsive. Distance-reduction comparisons are withheld if the two methods deliver different order sets. The optimizer is heuristic, not globally optimal. Closures are processed on simulation dispatch ticks, not live traffic events.

## Data and security notes

- `backend/data/` contains independent copies of the source CSVs and road geometry; `backend/src/` contains one shared copy of the original domain algorithms, with targeted integration and validation improvements. `project-status.md` records the analysis and implementation.
- Street geometry is cached from OpenStreetMap/OSRM. The original geometry has one polyline per undirected pair, so its reverse drawing is illustrative; route costs remain directional.
- This localhost API has no login, permissions, rate limiting, persistent storage, or production hardening. Do **not** expose it publicly or use the synthetic policy as an actual service agreement.
