"""Small local JSON API.

During development the frontend is served by Vite (which proxies ``/api``).
If ``frontend/dist`` exists (after ``npm run build``), this server also
serves the built app, so ``python run.py`` alone is enough for a demo.

Long computations are cancellable: every request gets a
:class:`~src.cancellation.CancelToken` which is cancelled when

* the browser disconnects (abort / tab closed / Vite proxy drops it),
* the client calls ``POST /api/cancel`` with the request's ``X-Request-Id``,
* the per-request time budget runs out.

Domain loops call ``checkpoint()`` and stop at the next safe point, so an
abandoned simulation does not keep a worker thread busy in the background.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import select
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from app.services import StudioService
from src.cancellation import CancelToken, Cancelled, use_token

MAX_BODY = 100_000
HEAVY_BUDGET_S = 45.0      # /api/plans and /api/fleet
LIGHT_BUDGET_S = 20.0      # everything else
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class CancelRegistry:
    """Request id -> live cancel token, for ``POST /api/cancel``."""

    def __init__(self) -> None:
        self._tokens: dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    def add(self, request_id: str | None, token: CancelToken) -> None:
        if request_id:
            with self._lock:
                self._tokens[request_id] = token

    def remove(self, request_id: str | None) -> None:
        if request_id:
            with self._lock:
                self._tokens.pop(request_id, None)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(request_id)
        if token is None:
            return False
        token.cancel("cancelled by client")
        return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._tokens)


def _watch_disconnect(sock: socket.socket, token: CancelToken,
                      done: threading.Event) -> None:
    """Cancel ``token`` if the peer closes the connection mid-computation."""
    while not done.is_set() and not token.cancelled:
        try:
            readable, _, _ = select.select([sock], [], [], 0.2)
            if readable and sock.recv(1, socket.MSG_PEEK) == b"":
                token.cancel("client disconnected")
                return
        except (OSError, ValueError):
            token.cancel("client disconnected")
            return


def handler_for(service: StudioService, registry: CancelRegistry,
                dist_dir: Path | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RouteStudio/1.1"

        # -- responses -------------------------------------------------
        def send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client went away; nothing left to tell it

        def run_cancellable(self, work: Callable[[], Any], budget_s: float) -> None:
            """Run ``work`` under a cancel token and send its JSON result."""
            raw_id = self.headers.get("X-Request-Id", "")
            request_id = raw_id if REQUEST_ID.match(raw_id or "") else None
            token = CancelToken(budget_s)
            done = threading.Event()
            registry.add(request_id, token)
            watcher = threading.Thread(target=_watch_disconnect,
                                       args=(self.connection, token, done), daemon=True)
            watcher.start()
            try:
                with use_token(token):
                    result = work()
            except Cancelled as stop:
                if stop.reason == "timeout":
                    self.send_json(408, {"error": f"The simulation exceeded the {budget_s:.0f}-second "
                                                  "time budget and was stopped on the server. "
                                                  "Reduce orders or drivers and try again.",
                                         "code": "TIMEOUT"})
                else:
                    self.send_json(499, {"error": "Request cancelled; the server stopped the computation.",
                                         "code": "CANCELLED"})
                return
            except (ValueError, TypeError, KeyError) as error:
                self.send_json(400, {"error": str(error)})
                return
            except Exception as error:  # noqa: BLE001 - always answer in JSON
                traceback.print_exc(file=sys.stderr)
                self.send_json(500, {"error": f"Internal simulator error ({type(error).__name__}). "
                                              "Check the backend terminal for details.",
                                     "code": "INTERNAL"})
                return
            finally:
                done.set()
                registry.remove(request_id)
            self.send_json(200, result)

        # -- static frontend ------------------------------------------
        def send_static(self, path: str) -> None:
            assert dist_dir is not None
            root = dist_dir.resolve()
            candidate = (root / unquote(path).lstrip("/")).resolve()
            if root not in candidate.parents and candidate != root or not candidate.is_file():
                candidate = root / "index.html"   # SPA route fallback
            data = candidate.read_bytes()
            kind = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            try:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                cache = "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable"
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # -- routing ---------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            params = parse_qs(url.query)

            def param(name: str, default: str = "") -> str:
                return params.get(name, [default])[0]

            routes: dict[str, tuple[Callable[[], Any], float]] = {
                "/api/health": (lambda: {"status": "ok", "active_requests": len(registry)}, LIGHT_BUDGET_S),
                "/api/catalog": (lambda: service.catalog_payload, LIGHT_BUDGET_S),
                "/api/graph": (lambda: service.graph_payload, LIGHT_BUDGET_S),
                "/api/coverage": (lambda: service.coverage(param("society")), LIGHT_BUDGET_S),
                "/api/serviceability": (lambda: service.serviceability(param("restaurant"), param("society")), LIGHT_BUDGET_S),
                "/api/search": (lambda: service.search(param("source"), param("target"), param("metric", "time")), LIGHT_BUDGET_S),
                "/api/fleet": (lambda: service.fleet(params), HEAVY_BUDGET_S),
            }
            if url.path in routes:
                work, budget = routes[url.path]
                self.run_cancellable(work, budget)
            elif url.path.startswith("/api/"):
                self.send_json(404, {"error": "API endpoint not found."})
            elif dist_dir is not None:
                self.send_static(url.path)
            else:
                self.send_json(404, {"error": "Frontend not built. Run `npm run dev` in frontend/ "
                                              "or `npm run build` to let this server host it."})

        def read_json(self) -> Any:
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self.send_json(415, {"error": "Send application/json."})
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 1 or length > MAX_BODY:
                self.send_json(413, {"error": "JSON body must be between 1 and 100000 bytes."})
                return None
            try:
                return json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self.send_json(400, {"error": "Invalid JSON body."})
                return None

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path not in ("/api/plans", "/api/cancel"):
                self.send_json(404, {"error": "API endpoint not found."})
                return
            data = self.read_json()
            if data is None:
                return
            if path == "/api/cancel":
                request_id = data.get("id") if isinstance(data, dict) else None
                if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
                    self.send_json(400, {"error": "Send {\"id\": \"<request id>\"}."})
                    return
                self.send_json(200, {"id": request_id, "cancelled": registry.cancel(request_id)})
                return
            self.run_cancellable(lambda: service.plan(data), HEAVY_BUDGET_S)

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    return Handler


class StudioServer(ThreadingHTTPServer):
    # On POSIX SO_REUSEADDR only lets a restarted server reclaim a port whose
    # old sockets are in TIME_WAIT (otherwise a quick restart fails with
    # "Address already in use"). On Windows it would allow two servers on
    # one port, so it stays off there.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def create_server(host: str = "127.0.0.1", port: int = 8060,
                  service: StudioService | None = None,
                  serve_frontend: bool = True) -> StudioServer:
    registry = CancelRegistry()
    dist = DIST_DIR if serve_frontend and (DIST_DIR / "index.html").is_file() else None
    server = StudioServer((host, port), handler_for(service or StudioService(), registry, dist))
    server.registry = registry  # type: ignore[attr-defined]
    server.serves_frontend = dist is not None  # type: ignore[attr-defined]
    return server
