"""DB tier: schema, CRUD, ordering."""
from watcher import db as dbmod


def test_seeds_and_empty(conn):
    assert dbmod.list_items(conn) == []


def test_add_and_list_newest_first(conn):
    a = dbmod.add_item(conn, "first")
    b = dbmod.add_item(conn, "second")
    rows = dbmod.list_items(conn)
    assert [r["id"] for r in rows] == [b["id"], a["id"]]
    assert rows[0]["text"] == "second" and rows[0]["done"] == 0


def test_set_item_whitelist_and_get(conn):
    it = dbmod.add_item(conn, "x")
    assert dbmod.set_item(conn, it["id"], {"done": 1})["done"] == 1
    assert dbmod.set_item(conn, it["id"], {})["text"] == "x"
    assert dbmod.get_item(conn, it["id"])["done"] == 1
    assert dbmod.get_item(conn, 9999) is None


def test_delete_returns_whether_it_existed(conn):
    it = dbmod.add_item(conn, "bye")
    assert dbmod.delete_item(conn, it["id"]) is True
    assert dbmod.delete_item(conn, it["id"]) is False
