"""Hermetic-tier fixtures: throwaway DB per test, live data never under test."""
import pytest

from watcher.db import connect, init_db
from watcher.web import create_app


@pytest.fixture()
def conn(tmp_path):
    c = connect(str(tmp_path / "test.sqlite"))
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def app(tmp_path):
    return create_app(str(tmp_path / "app.sqlite"))


@pytest.fixture()
def client(app):
    return app.test_client()
