"""Hermetic-tier fixtures: throwaway DB per test, live data never under test.

The scheduler thread is OFF in hermetic tests (start_scheduler=False); tests
that want it call tick_once() directly. Network knobs are pinned inert:
challenge wait 0, browser rungs disabled, so a stray fetch can't slow or
leak the suite.
"""
import copy

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
    a = create_app(str(tmp_path / "app.sqlite"), cfg=inert_cfg(),
                   start_scheduler=False)
    yield a
    net.reset_for_tests()


@pytest.fixture()
def client(app):
    return app.test_client()
