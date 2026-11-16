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


# ---- detector fitness: two-strike staleness + auto re-fit --------------------

BLIND_HTML = ("<html><head><title>Redesigned shop</title></head><body>"
              + "<p>The shop has been redesigned; nothing here mentions stock.</p>" * 8
              + "</body></html>")
assert len(BLIND_HTML) >= 300          # must read as a real page, not an outage stub
OUTAGE_HTML = "<html><body>503 Service Unavailable</body></html>"
# realistic-size restocked page: the re-fit sanity gate (rightly) refuses to
# fit a detector to a 48-char stub, so refit-success fixtures must look real
RESTOCK_REAL = ("<html><head><title>Double X film</title></head><body>"
                + "<p>In stock — add to cart</p>"
                + "<p>Graination camera shop, 35mm black and white film.</p>" * 5
                + "</body></html>")
assert len(RESTOCK_REAL) >= 300


def _no_llm(monkeypatch):
    def never(*a, **k):
        raise AssertionError("this path must not call the LLM")
    monkeypatch.setattr(llm, "chat_json", never)
    monkeypatch.setattr(llm, "chat", never)


def _refitting_watch(conn, spec=SPEC, notify_email=1, auto_refit_at=None):
    w = _ready_watch(conn, spec=spec, notify_email=notify_email)
    conn.execute("UPDATE watches SET status='refitting', auto_refit_at=? WHERE id=?",
                 (auto_refit_at or dbmod.now_iso(), w["id"]))
    conn.commit()
    return dbmod.get_watch(conn, w["id"])


def _llm_ok(monkeypatch):
    calls = {"n": 0}

    def fake(cfg, messages):
        calls["n"] += 1
        return {"mode": "condition", "present": ["In stock"],
                "absent": ["Out of stock"], "min_present": 1,
                "current_met": True, "notes": "refitted"}
    monkeypatch.setattr(llm, "chat_json", fake)
    return calls


def test_first_blind_poll_is_suspect_only(conn, cfg, monkeypatch):
    _no_llm(monkeypatch)
    sent = _sent(monkeypatch)
    w = _ready_watch(conn)
    _fetch_ok(monkeypatch, BLIND_HTML)
    up = check_once(cfg, conn, w)
    assert up["status"] == "ready"
    assert dbmod.list_checks(conn, w["id"])[0]["status"] == "suspect"
    assert sent == []                                   # no drama on 1/2


def test_two_consecutive_blind_polls_queue_refit(conn, cfg, monkeypatch):
    _no_llm(monkeypatch)
    w = _ready_watch(conn)
    _fetch_ok(monkeypatch, BLIND_HTML)
    check_once(cfg, conn, w)
    up = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "refitting" and up["auto_refit_at"] is not None
    assert dbmod.list_checks(conn, w["id"])[0]["status"] == "stale"
    # the queued watch is picked up by the onboarding pass
    assert [x["id"] for x in dbmod.pending_onboarding(conn)] == [w["id"]]


def test_outage_pages_never_count_toward_staleness(conn, cfg, monkeypatch):
    _no_llm(monkeypatch)
    w = _ready_watch(conn)
    _fetch_ok(monkeypatch, OUTAGE_HTML)
    check_once(cfg, conn, w)
    up = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    rows = dbmod.list_checks(conn, w["id"])
    assert up["status"] == "ready"                      # no refit on an outage
    assert rows[0]["status"] == "unreal" and rows[1]["status"] == "unreal"


def test_appearance_only_spec_is_never_blind(conn, cfg, monkeypatch):
    """present-only specs legitimately have zero hits while waiting."""
    _no_llm(monkeypatch)
    spec = {"mode": "condition", "present": ["In stock"], "absent": [],
            "min_present": 1, "notes": ""}
    w = _ready_watch(conn, spec=spec)
    _fetch_ok(monkeypatch, BLIND_HTML)
    check_once(cfg, conn, w)
    up = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "ready"
    rows = dbmod.list_checks(conn, w["id"])
    assert rows[0]["status"] == "ok" and rows[1]["status"] == "ok"


def test_refit_success_fits_new_spec_and_emails(conn, cfg, monkeypatch):
    from watcher import onboard
    w = _refitting_watch(conn)
    _fetch_ok(monkeypatch, RESTOCK_REAL)
    calls = _llm_ok(monkeypatch)
    sent = _sent(monkeypatch)
    up = onboard.onboard(cfg, conn, w)
    assert up["status"] == "ready" and "In stock" in up["spec_json"]
    assert calls["n"] == 1                              # exactly one LLM cycle
    assert len(sent) == 1 and "succeeded" in sent[0][0]


def test_refit_failure_keeps_old_spec_and_emails(conn, cfg, monkeypatch):
    from watcher import onboard
    w = _refitting_watch(conn)
    monkeypatch.setattr(net, "fetch", lambda url: FetchResult(
        url=url, status="error", error="connection refused"))
    sent = _sent(monkeypatch)
    up = onboard.onboard(cfg, conn, w)
    assert up["status"] == "needs_review"
    assert up["spec_json"] == w["spec_json"]            # old detector survives
    assert len(sent) == 1 and "FAILED" in sent[0][0]
    assert "old detector was kept" in sent[0][1]


def test_refit_refuses_unreal_page_and_alerts(conn, cfg, monkeypatch):
    """A re-fit that meets an outage or a NEW bot wall must not fit a spec to
    the interstitial — it alerts instead."""
    from watcher import onboard
    w = _refitting_watch(conn)
    _fetch_ok(monkeypatch, OUTAGE_HTML)
    _no_llm(monkeypatch)                                # LLM never sees garbage
    sent = _sent(monkeypatch)
    up = onboard.onboard(cfg, conn, w)
    assert up["status"] == "needs_review" and up["spec_json"] == w["spec_json"]
    assert len(sent) == 1 and "bot check" in sent[0][1]


def test_refit_cooldown_bounces_to_human(conn, cfg, monkeypatch):
    """Blind again within the cooldown window: no second LLM burn, needs_review."""
    _no_llm(monkeypatch)
    sent = _sent(monkeypatch)
    w = _ready_watch(conn)
    conn.execute("UPDATE watches SET auto_refit_at=? WHERE id=?",
                 (dbmod.now_iso(), w["id"]))
    conn.commit()
    w = dbmod.get_watch(conn, w["id"])
    _fetch_ok(monkeypatch, BLIND_HTML)
    check_once(cfg, conn, w)
    up = check_once(cfg, conn, dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "needs_review"
    assert len(sent) == 1 and "again within" in sent[0][1]


def test_refit_emails_ignore_per_watch_optout(conn, cfg, monkeypatch):
    """Operational alerts are not governed by the change-notification toggle."""
    from watcher import onboard
    w = _refitting_watch(conn, notify_email=0)
    _fetch_ok(monkeypatch, RESTOCK_REAL)
    _llm_ok(monkeypatch)
    sent = _sent(monkeypatch)
    onboard.onboard(cfg, conn, w)
    assert len(sent) == 1 and "succeeded" in sent[0][0]


def test_refit_crash_alerts_human_and_keeps_spec(conn, cfg, monkeypatch):
    from watcher import onboard
    w = _refitting_watch(conn)
    sent = _sent(monkeypatch)

    def boom(cfg, conn, watch):
        raise RuntimeError("llm exploded")
    monkeypatch.setattr(onboard, "onboard", boom)
    Scheduler(lambda: cfg, dbmod.path_of(conn)).tick_once()
    got = dbmod.get_watch(conn, w["id"])
    assert got["status"] == "needs_review" and got["spec_json"] == w["spec_json"]
    assert len(sent) == 1 and "crashed" in sent[0][1]
