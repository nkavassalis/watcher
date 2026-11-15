#!/usr/bin/env python3
"""Watcher — entrypoint.

config -> create_app -> run. Defaults to loopback; widening the bind is a
deliberate act (the perimeter owns auth), so it is not a CLI convenience here
beyond an explicit --host.
"""
import argparse

from watcher.web import create_app

DEFAULT_PORT = 8177


def main() -> None:
    ap = argparse.ArgumentParser(description="Watcher")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default="data/watcher.sqlite")
    args = ap.parse_args()

    app = create_app(args.db)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
