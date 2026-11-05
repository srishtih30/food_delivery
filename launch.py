"""Serve the freshly built app and open Fleet Lab using one local server."""
from pathlib import Path
import errno
import sys
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
from app.server import create_server


def main():
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Build the frontend first, or run start.bat.")
    try:
        server = create_server(port=8060)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
            raise
        # Another copy may be running. Never open an unrelated/older server.
        server = create_server(port=0)
    url = f"http://127.0.0.1:{server.server_port}/fleet"
    print(f"Route Studio is running: {url}", flush=True)
    print("Keep this window open. Press Ctrl+C to stop.", flush=True)
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
