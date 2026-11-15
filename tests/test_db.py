"""DB tier: watch CRUD, check recording, scheduling, host backoff, pruning."""
import json

from watcher import db as dbmod


def _mk(conn, url="https://example.com/p", intent="back in stock", iv=1440):
    return dbmod.create_watch(conn, url, intent, iv, 1)


def test_create_list_get(conn):
    assert dbmod.list_watches(conn) == []
    w = _mk(conn)
    assert w["status"] == "pending" and w["spec_json"] is None
    assert w["name"] is None and w["name_is_auto"] == 0   # unnamed: onboarding auto-guesses
    w2 = dbmod.create_watch(conn, "https://e.example/p", "i", 60, 1, name="Double X")
    assert w2["name"] == "Double X"
    assert w["interval_minutes"] == 1440 and w["notify_email"] == 1
    assert w["next_check_at"] <= dbmod.now_iso()          # due for onboarding now
    assert [r["id"] for r in dbmod.list_watches(conn)] == [w2["id"], w["id"]]
    assert dbmod.get_watch(conn, w["id"])["url"] == "https://example.com/p"
    assert dbmod.get_watch(conn, 999) is None


def test_name_migration_backfills(tmp_path):
    """Old DBs (no name column) gain the columns additively; a name that is
    just the url was never a real name and goes back to NULL for the
    auto-guesser, while genuine names survive with name_is_auto=0."""
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE watches (id INTEGER PRIMARY KEY, url TEXT NOT NULL,"
        " intent TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
        " spec_json TEXT, interval_minutes INTEGER NOT NULL DEFAULT 1440,"
        " notify_email INTEGER NOT NULL DEFAULT 1, baseline_hash TEXT,"
        " last_check_at TEXT, next_check_at TEXT, last_status TEXT,"
        " last_detail TEXT, fail_streak INTEGER NOT NULL DEFAULT 0,"
        " onboard_error TEXT, created_at TEXT, updated_at TEXT)")
    legacy.execute("INSERT INTO watches (url, intent) VALUES ('https://x.example/p', 'i')")
    legacy.execute("INSERT INTO watches (url, intent) VALUES ('https://y.example/p', 'i')")
    legacy.commit()
    legacy.close()
    c = dbmod.connect(path)
    dbmod.init_db(c)                      # runs _migrate
    rows = {r["url"]: r for r in dbmod.list_watches(c)}
    assert rows["https://x.example/p"]["name"] is None       # url-name handed to the guesser
    dbmod.update_watch(c, rows["https://y.example/p"]["id"], {"name": "Keeper"})
    dbmod.init_db(c)                      # idempotent re-run must not clobber it
    assert dbmod.get_watch(c, rows["https://y.example/p"]["id"])["name"] == "Keeper"
    c.close()


def test_update_whitelist_and_delete(conn):
    w = _mk(conn)
    assert dbmod.update_watch(conn, w["id"], {"interval_minutes": 60})["interval_minutes"] == 60
    assert dbmod.update_watch(conn, w["id"], {})["interval_minutes"] == 60
    assert dbmod.update_watch(conn, 999, {"interval_minutes": 5}) is None
    assert dbmod.delete_watch(conn, w["id"]) is True
    assert dbmod.delete_watch(conn, w["id"]) is False


def test_set_spec(conn):
    w = _mk(conn)
    spec = {"mode": "condition", "present": ["In stock"], "absent": ["Out of stock"]}
    up = dbmod.set_spec(conn, w["id"], json.dumps(spec), "ready")
    assert up["status"] == "ready" and json.loads(up["spec_json"]) == spec
    up = dbmod.set_spec(conn, w["id"], None, "needs_review", "llm said no")
    assert up["status"] == "needs_review" and up["onboard_error"] == "llm said no"


def test_due_watches_filters(conn):
    w = _mk(conn)
    dbmod.set_spec(conn, w["id"], "{}", "ready")
    dbmod.schedule_next(conn, w["id"], 0)
    assert [x["id"] for x in dbmod.due_watches(conn, dbmod.now_iso())] == [w["id"]]
    dbmod.schedule_next(conn, w["id"], 1440)
    assert dbmod.due_watches(conn, dbmod.now_iso()) == []
    dbmod.update_watch(conn, w["id"], {"status": "paused"})
    dbmod.schedule_next(conn, w["id"], 0)
    assert dbmod.due_watches(conn, dbmod.now_iso()) == []


def test_due_watches_respects_host_block(conn):
    w = _mk(conn, url="https://blocked.example/p")
    dbmod.set_spec(conn, w["id"], "{}", "ready")
    dbmod.schedule_next(conn, w["id"], 0)
    dbmod.note_host_block(conn, "blocked.example", 60, 600)
    assert dbmod.due_watches(conn, dbmod.now_iso()) == []
    dbmod.clear_host_block(conn, "blocked.example")
    assert len(dbmod.due_watches(conn, dbmod.now_iso())) == 1


def test_record_check_bookkeeping(conn):
    w = _mk(conn)
    dbmod.record_check(conn, w["id"], "error", detail="boom")
    got = dbmod.get_watch(conn, w["id"])
    assert got["fail_streak"] == 1 and got["last_status"] == "error"
    dbmod.record_check(conn, w["id"], "error", detail="boom")
    assert dbmod.get_watch(conn, w["id"])["fail_streak"] == 2
    dbmod.record_check(conn, w["id"], "ok", rung=0, detail="fine",
                       baseline_hash="abc")
    got = dbmod.get_watch(conn, w["id"])
    assert got["fail_streak"] == 0 and got["baseline_hash"] == "abc"
    # baseline_hash=None must not clobber an existing baseline
    dbmod.record_check(conn, w["id"], "ok", baseline_hash=None)
    assert dbmod.get_watch(conn, w["id"])["baseline_hash"] == "abc"
    checks = dbmod.list_checks(conn, w["id"])
    assert [c["status"] for c in checks] == ["ok", "ok", "error", "error"]


def test_host_backoff_doubles_and_caps(conn):
    d1 = dbmod.note_host_block(conn, "x.example", 60, 500)
    d2 = dbmod.note_host_block(conn, "x.example", 60, 500)
    d3 = dbmod.note_host_block(conn, "x.example", 60, 500)
    assert (d1, d2, d3) == (60, 120, 240)
    assert dbmod.note_host_block(conn, "x.example", 60, 500) == 480
    assert dbmod.note_host_block(conn, "x.example", 60, 500) == 500   # capped
    assert dbmod.host_blocked(conn, "x.example", dbmod.now_iso())
    assert not dbmod.host_blocked(conn, "other.example", dbmod.now_iso())


def test_prune_checks_keeps_newest(conn):
    w = _mk(conn)
    for _ in range(10):
        dbmod.record_check(conn, w["id"], "ok")
    assert dbmod.prune_checks(conn, keep_per_watch=4) == 6
    assert len(dbmod.list_checks(conn, w["id"])) == 4


def test_delete_watch_cascades_checks(conn):
    w = _mk(conn)
    dbmod.record_check(conn, w["id"], "ok")
    dbmod.delete_watch(conn, w["id"])
    assert dbmod.list_checks(conn, w["id"]) == []
