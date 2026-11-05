"""Launch the local Route Studio API."""

import argparse

from app.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Route Studio local API")
    parser.add_argument("--port", type=int, default=8060)
    args = parser.parse_args()
    server = create_server(port=args.port)
    print(f"Route Studio API listening at http://127.0.0.1:{server.server_port}", flush=True)
    if getattr(server, "serves_frontend", False):
        print(f"Built frontend (frontend/dist) served at http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
