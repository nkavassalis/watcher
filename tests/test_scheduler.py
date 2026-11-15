"""Scheduler + check_once: the single pending-vs-error decision point,
edge-triggered email, host backoff wiring, error retry cap, and the
zero-LLM-calls-on-poll token discipline.
"""
import json

import pytest
from conftest import inert_cfg

from watcher import db as dbmod
from watcher import llm, net, notify
from watcher.net import FetchResult
from watcher.scheduler import Scheduler, check_once

STOCK_HTML = "<html><body><p>Out of stock</p></body></html>"
RESTOCK_HTML = "<html><body><p>In stock — add to cart</p></body></html>"

SPEC = {"mode": "condition", "present": ["In stock"], "absent": ["Out of stock"],
        "min_present": 1, "notes": ""}
HASH_SPEC = {"mode": "hash", "present": [], "absent": [], "min_present": 1, "notes": ""}


@pytest.fixture()
def cfg():
    c = inert_cfg()
    c["email"].update({"enabled": True, "to": "nick@example.com"})
    return c


def _ready_watch(conn, spec=SPEC, url="https://x.example/p", iv=1440, notify_email=1):
    w = dbmod.create_watch(conn, url, "back in stock", iv, notify_email)
    dbmod.set_spec(conn, w["id"], json.dumps(spec), "ready")
    return dbmod.get_watch(conn, w["id"])


def _fetch_ok(monkeypatch, html, rung=0):
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="ok", code=200,
                                                text=html, rung=rung))


def _sent(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda cfg, subject, body, html=False: sent.append(
                            (subject, body)) or "msgid1")
    return sent


# ---- happy paths -------------------------------------------------------------

def test_unchanged_check_schedules_next_interval(conn, cfg, monkeypatch):
    w = _ready_watch(conn)
    _fetch_ok(monkeypatch, STOCK_HTML)
    up = check_once(cfg, conn, w)
    assert up["last_status"] == "ok" and up["fail_streak"] == 0
    assert up["next_check_at"] > dbmod.now_iso()          # ~24h out
    assert dbmod.list_checks(conn, w["id"])[0]["rung"] == 0


def test_change_triggers_once_email_on_transition(conn, cfg, monkeypatch):
    w = _ready_watch(conn)
    sent = _sent(monkeypatch)
    _fetch_ok(monkeypatch, RESTOCK_HTML)
    up = check_once(cfg, conn, w)
    assert up["last_status"] == "changed" and len(sent) == 1
    assert "x.example" in sent[0][0] and "emailed" in up["last_detail"]
    # still in stock next check: condition remains met -> NO second email (edge trigger)
    up2 = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    assert up2["last_status"] == "changed" and len(sent) == 1


def test_hash_mode_emails_every_change(conn, cfg, monkeypatch):
    w = _ready_watch(conn, spec=HASH_SPEC)
    sent = _sent(monkeypatch)
    _fetch_ok(monkeypatch, "<p>version 1</p>")
    check_once(cfg, conn, w)                                # baseline, no email
    _fetch_ok(monkeypatch, "<p>version 2</p>")
    check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    _fetch_ok(monkeypatch, "<p>version 3</p>")
    up = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    assert len(sent) == 2 and up["last_status"] == "changed"


def test_notify_email_off_sends_nothing(conn, cfg, monkeypatch):
    w = _ready_watch(conn, notify_email=0)
    sent = _sent(monkeypatch)
    _fetch_ok(monkeypatch, RESTOCK_HTML)
    check_once(cfg, conn, w)
    assert sent == []


def test_email_failure_is_recorded_not_fatal(conn, cfg, monkeypatch):
    w = _ready_watch(conn)

    def boom(cfg_, subject, body, html=False):
        raise notify.NotifyError("gmail tokens missing")

    monkeypatch.setattr(notify, "send", boom)
    _fetch_ok(monkeypatch, RESTOCK_HTML)
    up = check_once(cfg, conn, w)
    assert up["last_status"] == "changed"           # the check itself succeeded
    assert "EMAIL FAILED" in up["last_detail"]


# ---- failure paths -------------------------------------------------------------

def test_error_caps_retry_at_one_hour(conn, cfg, monkeypatch):
    w = _ready_watch(conn, iv=10080)                 # weekly watch, transient error
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="error", error="timeout"))
    up = check_once(cfg, conn, w)
    assert up["last_status"] == "error" and up["fail_streak"] == 1
    # next check within the hour, not next week
    assert up["next_check_at"] <= dbmod.iso_plus(61)


def test_blocked_pauses_the_host(conn, cfg, monkeypatch):
    w = _ready_watch(conn, url="https://mean.example/p")
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="blocked", code=403))
    up = check_once(cfg, conn, w)
    assert up["last_status"] == "blocked"
    assert dbmod.host_blocked(conn, "mean.example", dbmod.now_iso())
    assert dbmod.due_watches(conn, dbmod.now_iso()) == []


def test_blocked_host_backoff_doubles(conn, cfg, monkeypatch):
    w = _ready_watch(conn, url="https://mean.example/p")
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="blocked", code=429))
    check_once(cfg, conn, w)
    row = conn.execute("SELECT fail_streak FROM hosts WHERE host='mean.example'").fetchone()
    assert row["fail_streak"] == 1
    check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    row = conn.execute("SELECT fail_streak FROM hosts WHERE host='mean.example'").fetchone()
    assert row["fail_streak"] == 2


def test_challenged_keeps_user_interval(conn, cfg, monkeypatch):
    w = _ready_watch(conn)
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="challenged", code=403))
    up = check_once(cfg, conn, w)
    assert up["last_status"] == "challenged"
    assert up["next_check_at"] > dbmod.iso_plus(1439)   # waiting IS the retry


def test_successful_fetch_clears_host_block(conn, cfg, monkeypatch):
    w = _ready_watch(conn, url="https://grumpy.example/p")
    dbmod.note_host_block(conn, "grumpy.example", 60, 60)
    _fetch_ok(monkeypatch, STOCK_HTML)
    check_once(cfg, conn, w)
    assert not dbmod.host_blocked(conn, "grumpy.example", dbmod.now_iso())


def test_corrupt_spec_sends_watch_to_needs_review(conn, cfg, monkeypatch):
    w = _ready_watch(conn)
    conn.execute("UPDATE watches SET spec_json='{broken' WHERE id=?", (w["id"],))
    conn.commit()
    _fetch_ok(monkeypatch, STOCK_HTML)
    up = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "needs_review"


# ---- token discipline -------------------------------------------------------------

def test_periodic_check_makes_zero_llm_calls(conn, cfg, monkeypatch):
    def never(*a, **k):
        raise AssertionError("poll path must not call the LLM")

    monkeypatch.setattr(llm, "chat", never)
    monkeypatch.setattr(llm, "chat_json", never)
    monkeypatch.setattr(llm, "_post", never)
    w = _ready_watch(conn)
    _fetch_ok(monkeypatch, RESTOCK_HTML)
    check_once(cfg, conn, w)                          # must not raise
    _fetch_ok(monkeypatch, STOCK_HTML)
    check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))


# ---- scheduler loop -------------------------------------------------------------

def test_tick_once_onboards_pending_and_checks_due(conn, cfg, monkeypatch):
    pending = dbmod.create_watch(conn, "https://a.example/p", "i", 1440, 0)
    due = _ready_watch(conn, url="https://b.example/p")
    dbmod.schedule_next(conn, due["id"], 0)
    calls = []
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="ok", code=200,
                                                text=STOCK_HTML, rung=0))

    def fake_onboard(cfg_, conn_, w):
        calls.append(("onboard", w["id"]))
        return dbmod.set_spec(conn_, w["id"], json.dumps(SPEC), "ready")

    import watcher.scheduler as schedmod
    monkeypatch.setattr(schedmod.onboard, "onboard", fake_onboard)
    s = Scheduler(lambda: cfg, dbmod.path_of(conn))
    s.tick_once()
    assert ("onboard", pending["id"]) in calls
    assert dbmod.get_watch(conn, pending["id"])["status"] == "ready"
    assert dbmod.get_watch(conn, due["id"])["last_status"] == "ok"


def test_tick_once_survives_crashing_check(conn, cfg, monkeypatch):
    w = _ready_watch(conn)
    dbmod.schedule_next(conn, w["id"], 0)

    def boom(url):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(net, "fetch", boom)
    s = Scheduler(lambda: cfg, dbmod.path_of(conn))
    s.tick_once()                                     # must not raise
    got = dbmod.get_watch(conn, w["id"])
    assert got["last_status"] == "error" and "check crashed" in got["last_detail"]
