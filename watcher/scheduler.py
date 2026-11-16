"""In-process scheduler thread: onboards pending watches, polls due ones.

One connection per thread (house invariant 3): the thread opens its own via
db.connect(db.path_of(...)) and never shares the request connection. Config is
re-read from the live dict each tick so settings edits apply without restart.
"""
from __future__ import annotations

import json
import logging
import threading

from . import db, evaluate, net, notify, onboard

log = logging.getLogger("watcher.scheduler")

ERROR_RETRY_CAP_MINUTES = 60          # an errored check retries within the hour at most
REFIT_COOLDOWN_HOURS = 24             # stale twice within a day -> human, not LLM


def _refit_email(cfg: dict, watch: dict, ok: bool, reason: str) -> None:
    """Operational alert, independent of the per-watch notify_email toggle.
    Never raises: a dead mailbox must not derail the scheduler."""
    try:
        mid = notify.notify_refit(cfg, watch, ok, reason)
        if mid:
            log.info("refit alert (%s) emailed for watch %s: %s",
                     "ok" if ok else "fail", watch["id"], mid)
    except notify.NotifyError as e:
        log.warning("refit alert email failed for watch %s: %s", watch["id"], e)


def _begin_refit(cfg: dict, conn, watch: dict) -> dict:
    """Second consecutive blind poll of a REAL page: the detector is unfit.
    Queue an auto re-fit (old spec kept until the new one validates) — unless
    we just re-fit inside the cooldown, which means the LLM already tried and
    the page is beyond automatic help."""
    last = watch.get("auto_refit_at")
    if last and last > db.iso_minus(REFIT_COOLDOWN_HOURS):
        db.record_check(conn, watch["id"], "stale",
                        detail=f"blind again within {REFIT_COOLDOWN_HOURS}h of the "
                               "last auto-refit; needs review")
        updated = db.set_spec(conn, watch["id"], watch["spec_json"], "needs_review",
                              "detector keeps going blind after auto-refit")
        _refit_email(cfg, watch, False,
                     f"the detector went blind again within {REFIT_COOLDOWN_HOURS} "
                     "hours of the last auto-refit — the automatic re-fit is not "
                     "keeping up; review the watch in the UI")
        return updated
    db.record_check(conn, watch["id"], "stale",
                    detail="detector blind — no needle found on two consecutive "
                           "polls; auto-refit queued")
    db.mark_refitting(conn, watch["id"])
    return db.get_watch(conn, watch["id"])


def check_once(cfg: dict, conn, watch: dict) -> dict:
    """Run one poll of one watch. THE decision point for pending-vs-error —
    never fork this logic. Returns the updated watch row."""
    result = net.fetch(watch["url"])
    host = db._host_of(watch["url"])

    if result.status == "blocked":
        delay = db.note_host_block(conn, host,
                                   cfg["fetch"]["block_backoff_base_seconds"],
                                   cfg["fetch"]["block_backoff_max_seconds"])
        detail = f"blocked (HTTP {result.code}); host paused {delay}s"
        db.record_check(conn, watch["id"], "blocked", rung=result.rung, detail=detail)
        db.schedule_next(conn, watch["id"], watch["interval_minutes"])
        return db.get_watch(conn, watch["id"])

    if result.status == "challenged":
        # a challenge is not a ban: waiting IS the retry, so keep the user interval
        db.record_check(conn, watch["id"], "challenged", rung=result.rung,
                        detail="bot check not cleared; will try again next cycle")
        db.schedule_next(conn, watch["id"], watch["interval_minutes"])
        return db.get_watch(conn, watch["id"])

    if not result.ok:
        db.record_check(conn, watch["id"], "error", rung=result.rung,
                        detail=result.error or "fetch failed")
        db.schedule_next(conn, watch["id"],
                         min(watch["interval_minutes"], ERROR_RETRY_CAP_MINUTES))
        return db.get_watch(conn, watch["id"])

    db.clear_host_block(conn, host)
    try:
        spec = json.loads(watch["spec_json"]) if watch["spec_json"] else None
    except json.JSONDecodeError:
        spec = None
    if not isinstance(spec, dict) or evaluate.validate_spec(spec):
        db.record_check(conn, watch["id"], "error", rung=result.rung,
                        detail="stored spec is invalid; re-onboard needed")
        return db.set_spec(conn, watch["id"], None, "needs_review",
                           "stored spec is invalid")

    ev = evaluate.evaluate(spec, result.text, watch.get("baseline_hash"))
    detail = ev["detail"]

    # --- detector fitness: two-strike staleness --------------------------------
    # A condition spec with absent needles that finds NEITHER signal on the
    # page is blind (the site redesigned around it). One blind poll of a real
    # page is only a 'suspect' check row; a second consecutive one queues an
    # auto re-fit. Pages that look like an outage or a soft bot wall (200
    # interstitial) are recorded as 'unreal' and never count — an outage must
    # not burn the detector or fire an LLM call.
    if ev["status"] == "ok" and ev.get("blind"):
        if net.looks_unreal(result.code, result.text):
            db.record_check(conn, watch["id"], "unreal", rung=result.rung,
                            detail="page looks like an outage/bot-wall (HTTP 200 "
                                   "interstitial); not counted against the detector")
            db.schedule_next(conn, watch["id"], watch["interval_minutes"])
            return db.get_watch(conn, watch["id"])
        if db.last_check_status(conn, watch["id"]) == "suspect":
            return _begin_refit(cfg, conn, watch)
        db.record_check(conn, watch["id"], "suspect", rung=result.rung,
                        detail="no detector needle found on the page (1/2); one "
                               "more blind poll re-fits the detector",
                        matched=json.dumps(ev["matched"]) if ev["matched"] else None,
                        snapshot_hash=ev["snapshot_hash"])
        db.schedule_next(conn, watch["id"], watch["interval_minutes"])
        return db.get_watch(conn, watch["id"])

    # edge-triggered email: condition specs fire once per transition into
    # "changed"; hash specs fire on every real change.
    if (ev["status"] == "changed" and watch["notify_email"]
            and (spec["mode"] == "hash" or watch["last_status"] != "changed")):
        try:
            mid = notify.notify_change(cfg, watch, detail)
            if mid:
                detail += f"; emailed {cfg['email']['to']} ({mid})"
        except notify.NotifyError as e:
            detail += f"; EMAIL FAILED: {e}"
            log.warning("notify failed for watch %s: %s", watch["id"], e)

    db.record_check(
        conn, watch["id"], ev["status"], rung=result.rung, detail=detail,
        matched=json.dumps(ev["matched"]) if ev["matched"] else None,
        snapshot_hash=ev["snapshot_hash"], baseline_hash=ev["baseline_hash"])
    db.schedule_next(conn, watch["id"], watch["interval_minutes"])
    return db.get_watch(conn, watch["id"])


class Scheduler(threading.Thread):
    """Daemon loop. stop() is graceful; exceptions never kill the thread."""

    def __init__(self, get_cfg, db_path: str, tick_seconds: float | None = None):
        super().__init__(daemon=True, name="watcher-scheduler")
        self._get_cfg = get_cfg
        self._db_path = db_path
        self._tick = tick_seconds
        self._stop = threading.Event()
        self._ticks = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception:
                log.exception("scheduler tick failed")
            tick = self._tick or self._get_cfg()["scheduler"]["tick_seconds"]
            self._stop.wait(max(1, tick))
        log.info("scheduler stopped")

    def tick_once(self) -> None:
        cfg = self._get_cfg()
        net.configure(cfg)
        conn = db.connect(self._db_path)
        try:
            for w in db.pending_onboarding(conn):
                try:
                    updated = onboard.onboard(cfg, conn, w)
                    if updated and updated["status"] == "ready":
                        db.schedule_next(conn, w["id"], w["interval_minutes"])
                except Exception:
                    log.exception("onboarding failed for watch %s", w["id"])
                    if w["status"] == "refitting":
                        # an auto re-fit crashed: keep the old spec, alert,
                        # and hand it to a human — never silently drop back
                        db.set_spec(conn, w["id"], w["spec_json"], "needs_review",
                                    "auto-refit crashed")
                        _refit_email(cfg, w, False,
                                     "the automatic re-fit crashed (see server log); "
                                     "the old detector was kept and the watch is "
                                     "paused for review")
                    else:
                        db.set_spec(conn, w["id"], None, "needs_review",
                                    "onboarding crashed")
            now = db.now_iso()
            for w in db.due_watches(conn, now):
                try:
                    check_once(cfg, conn, w)
                except Exception:
                    log.exception("check failed for watch %s", w["id"])
                    db.record_check(conn, w["id"], "error", detail="check crashed")
                    db.schedule_next(conn, w["id"],
                                     min(w["interval_minutes"], ERROR_RETRY_CAP_MINUTES))
            self._ticks += 1
            if self._ticks % 100 == 1:
                db.prune_checks(conn)
        finally:
            conn.close()
