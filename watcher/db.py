"""Watcher — all SQL lives in this module. Routes never touch SQL."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id         INTEGER PRIMARY KEY,
  text       TEXT NOT NULL,
  done       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_items_sort ON items (created_at DESC, id DESC);
"""


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


def list_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, text, done, created_at FROM items ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def add_item(conn: sqlite3.Connection, text: str) -> dict:
    cur = conn.execute("INSERT INTO items (text) VALUES (?)", (text,))
    conn.commit()
    row = conn.execute(
        "SELECT id, text, done, created_at FROM items WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def set_item(conn: sqlite3.Connection, item_id: int, fields: dict) -> dict | None:
    """fields is already whitelisted by the web layer (text, done)."""
    if not fields:
        return get_item(conn, item_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))
    conn.commit()
    return get_item(conn, item_id)


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, text, done, created_at FROM items WHERE id=?", (item_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_item(conn: sqlite3.Connection, item_id: int) -> bool:
    cur = conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    return cur.rowcount > 0
