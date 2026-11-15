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
                    db.set_spec(conn, w["id"], None, "needs_review", "onboarding crashed")
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
