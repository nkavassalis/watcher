"""Hermetic-tier fixtures: throwaway DB per test, live data never under test.

WATCHER_TEST_MODE=1 is set for the whole suite: create_app() refuses to save
settings to the implicit ./config.json under it. Without this guard a hermetic
PUT /api/settings overwrote the repo's real config.json (2026-11-15) and the
restart that followed loaded the live instance with test values — "we use a
throwaway db" must extend to throwaway CONFIG too (app fixtures also pass an
explicit tmp config_path).

The scheduler thread is OFF in hermetic tests (start_scheduler=False); tests
that want it call tick_once() directly. Network knobs are pinned inert:
challenge wait 0, browser rungs disabled, so a stray fetch can't slow or
leak the suite.
"""
import copy
import os

os.environ["WATCHER_TEST_MODE"] = "1"

import pytest

from watcher import config as configmod
from watcher import net
from watcher.db import connect, init_db
from watcher.web import create_app


def inert_cfg() -> dict:
    cfg = copy.deepcopy(configmod.DEFAULTS)
    cfg["fetch"]["challenge_wait_seconds"] = 0
    cfg["fetch"]["browser_enabled"] = False
    cfg["fetch"]["block_backoff_base_seconds"] = 60
    cfg["fetch"]["block_backoff_max_seconds"] = 600
    cfg["email"]["enabled"] = False
    return cfg


@pytest.fixture()
def conn(tmp_path):
    c = connect(str(tmp_path / "test.sqlite"))
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def app(tmp_path):
    a = create_app(str(tmp_path / "app.sqlite"),
                   config_path=str(tmp_path / "config.json"),   # never ./config.json
                   cfg=inert_cfg(), start_scheduler=False)
    yield a
    net.reset_for_tests()


@pytest.fixture()
def client(app):
    return app.test_client()
