"""CLI entry — also the `python -m watcher` entrypoint.

Long-running services on this box must launch as `python -m watcher`, never
`python run.py`: process-name-based pkill from other projects must not be able
to hit this daemon by a `run.py` pattern match.
"""
import argparse
import logging

from watcher.web import create_app

DEFAULT_PORT = 8177


def main() -> None:
    ap = argparse.ArgumentParser(description="Watcher")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; the app has no auth — LAN/perimeter only")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default="data/watcher.sqlite")
    ap.add_argument("--config", default=None,
                    help="config JSON (default: ./config.json if present)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    app = create_app(args.db, config_path=args.config)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
