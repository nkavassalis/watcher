"""Watcher — all SQL lives in this module. Routes never touch SQL.

Timestamps are UTC ISO strings ('%Y-%m-%dT%H:%M:%SZ'); they sort lexicographically,
so `next_check_at <= now` works as a plain string comparison.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
  id               INTEGER PRIMARY KEY,
  url              TEXT NOT NULL,
  intent           TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending|ready|needs_review|paused
  spec_json        TEXT,                              -- detection spec produced at onboarding
  interval_minutes INTEGER NOT NULL DEFAULT 1440,
  notify_email     INTEGER NOT NULL DEFAULT 1,
  baseline_hash    TEXT,                              -- hash-mode content fingerprint
  last_check_at    TEXT,
  next_check_at    TEXT,
  last_status      TEXT,                              -- ok|changed|unchanged|error|challenged|blocked
  last_detail      TEXT,
  fail_streak      INTEGER NOT NULL DEFAULT 0,
  onboard_error    TEXT,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_watches_due ON watches (status, next_check_at);

CREATE TABLE IF NOT EXISTS checks (
  id            INTEGER PRIMARY KEY,
  watch_id      INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  ts            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  status        TEXT NOT NULL,          -- ok|changed|unchanged|error|challenged|blocked
  rung          INTEGER,                -- fetch ladder rung that succeeded (0-4), NULL if never fetched
  detail        TEXT,
  matched_json  TEXT,                   -- which needles matched (deterministic, no LLM)
  snapshot_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_watch ON checks (watch_id, ts DESC, id DESC);

CREATE TABLE IF NOT EXISTS hosts (
  host           TEXT PRIMARY KEY,
  blocked_until  TEXT,
  fail_streak    INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

WATCH_FIELDS = ("url", "intent", "interval_minutes", "notify_email", "status")

# statuses that count as "the source is refusing us" -> host backoff
BLOCKED_STATUSES = ("blocked",)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_plus(minutes: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def path_of(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA database_list").fetchall()
    return rows[0][2] if rows else ":memory:"


# ---- watches ---------------------------------------------------------------

def create_watch(conn: sqlite3.Connection, url: str, intent: str,
                 interval_minutes: int, notify_email: int) -> dict:
    cur = conn.execute(
        "INSERT INTO watches (url, intent, interval_minutes, notify_email, next_check_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (url, intent, interval_minutes, notify_email, now_iso()),  # due immediately: onboarding
    )
    conn.commit()
    return get_watch(conn, cur.lastrowid)


def get_watch(conn: sqlite3.Connection, watch_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    return dict(row) if row else None


def list_watches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM watches ORDER BY created_at DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def update_watch(conn: sqlite3.Connection, watch_id: int, fields: dict) -> dict | None:
    """fields is already whitelisted by the web layer (WATCH_FIELDS subset)."""
    if not fields:
        return get_watch(conn, watch_id)
    fields = dict(fields)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), watch_id))
    conn.commit()
    return get_watch(conn, watch_id)


def delete_watch(conn: sqlite3.Connection, watch_id: int) -> bool:
    cur = conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
    conn.commit()
    return cur.rowcount > 0


def set_spec(conn: sqlite3.Connection, watch_id: int, spec_json: str | None,
             status: str, error: str | None = None) -> dict | None:
    """Onboarding outcome: ready with a spec, or needs_review with the reason."""
    conn.execute(
        "UPDATE watches SET spec_json=?, status=?, onboard_error=?, updated_at=?"
        " WHERE id=?",
        (spec_json, status, error, now_iso(), watch_id))
    conn.commit()
    return get_watch(conn, watch_id)


# ---- scheduling --------------------------------------------------------------

def due_watches(conn: sqlite3.Connection, now: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM watches WHERE status='ready' AND next_check_at IS NOT NULL"
        " AND next_check_at <= ? ORDER BY next_check_at, id", (now,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not host_blocked(conn, _host_of(d["url"]), now):
            out.append(d)
    return out


def pending_onboarding(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM watches WHERE status='pending' ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


def schedule_next(conn: sqlite3.Connection, watch_id: int, minutes: int) -> None:
    conn.execute("UPDATE watches SET next_check_at=? WHERE id=?",
                 (iso_plus(minutes), watch_id))
    conn.commit()


def record_check(conn: sqlite3.Connection, watch_id: int, status: str,
                 rung: int | None = None, detail: str | None = None,
                 matched: str | None = None, snapshot_hash: str | None = None,
                 baseline_hash: str | None = None) -> dict:
    """THE one place a check outcome is written: check row + watch bookkeeping."""
    conn.execute(
        "INSERT INTO checks (watch_id, status, rung, detail, matched_json, snapshot_hash)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (watch_id, status, rung, detail, matched, snapshot_hash))
    streak_row = conn.execute("SELECT fail_streak FROM watches WHERE id=?",
                              (watch_id,)).fetchone()
    streak = (streak_row["fail_streak"] if streak_row else 0)
    streak = streak + 1 if status == "error" else 0
    conn.execute(
        "UPDATE watches SET last_check_at=?, last_status=?, last_detail=?,"
        " fail_streak=?, baseline_hash=COALESCE(?, baseline_hash), updated_at=?"
        " WHERE id=?",
        (now_iso(), status, detail, streak, baseline_hash, now_iso(), watch_id))
    conn.commit()
    return get_watch(conn, watch_id)


def list_checks(conn: sqlite3.Connection, watch_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, status, rung, detail, matched_json, snapshot_hash"
        " FROM checks WHERE watch_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (watch_id, limit)).fetchall()
    return [dict(r) for r in rows]


def prune_checks(conn: sqlite3.Connection, keep_per_watch: int = 500) -> int:
    cur = conn.execute(
        "DELETE FROM checks WHERE id NOT IN ("
        "  SELECT id FROM checks c2 WHERE c2.watch_id = checks.watch_id"
        "  ORDER BY ts DESC, id DESC LIMIT ?)", (keep_per_watch,))
    conn.commit()
    return cur.rowcount


# ---- per-host backoff (403/429 pauses the source, doubling) --------------------

def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).netloc or "?").lower()


def host_blocked(conn: sqlite3.Connection, host: str, now: str) -> bool:
    row = conn.execute("SELECT blocked_until FROM hosts WHERE host=?",
                       (host,)).fetchone()
    return bool(row and row["blocked_until"] and row["blocked_until"] > now)


def note_host_block(conn: sqlite3.Connection, host: str, base_seconds: int,
                    max_seconds: int) -> int:
    row = conn.execute("SELECT fail_streak FROM hosts WHERE host=?",
                       (host,)).fetchone()
    streak = (row["fail_streak"] if row else 0) + 1
    delay = min(base_seconds * (2 ** (streak - 1)), max_seconds)
    blocked = (datetime.now(UTC) + timedelta(seconds=delay)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO hosts (host, blocked_until, fail_streak, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(host) DO UPDATE SET blocked_until=excluded.blocked_until,"
        " fail_streak=excluded.fail_streak, updated_at=excluded.updated_at",
        (host, blocked, streak, now_iso()))
    conn.commit()
    return delay


def clear_host_block(conn: sqlite3.Connection, host: str) -> None:
    conn.execute("UPDATE hosts SET blocked_until=NULL, fail_streak=0, updated_at=?"
                 " WHERE host=?", (now_iso(), host))
    conn.commit()
