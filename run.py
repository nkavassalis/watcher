#!/usr/bin/env python3
"""Watcher — thin wrapper so `./run.py` keeps working. The real entrypoint is
watcher/cli.py (`python -m watcher` is the supported daemon launch)."""
from watcher.cli import main

if __name__ == "__main__":
    main()
