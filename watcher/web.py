"""Watcher — Flask app factory. Thin routes, no SQL, JSON errors always."""
from __future__ import annotations

import sqlite3

from flask import Flask, jsonify, render_template, request

from . import db as dbmod

APP_VERSION = "0.1.0"


def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path

    def conn() -> sqlite3.Connection:
        # one connection per request/thread; never share across threads
        return dbmod.connect(app.config["DB_PATH"])

    dbmod.init_db(conn())

    # ---- input hygiene ---------------------------------------------------
    def _body() -> dict | None:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else None

    def _str(data: dict, key: str, *, maxlen: int = 500) -> str | None:
        v = data.get(key)
        if not isinstance(v, str):
            return None
        v = " ".join(v.split())
        return v[:maxlen] if v else None

    def _bool(data: dict, key: str) -> int | None:
        v = data.get(key)
        if isinstance(v, bool):
            return int(v)
        if v in (0, 1):
            return int(v)
        return None

    # JSON, never an HTML 500, under /api/
    @app.errorhandler(Exception)
    def _api_error(e):
        if request.path.startswith("/api/"):
            code = getattr(e, "code", 500)
            code = code if isinstance(code, int) and 400 <= code < 600 else 500
            return jsonify(error=str(getattr(e, "description", e) or "error")), code
        raise e

    # ---- pages ------------------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html", app_version=APP_VERSION)

    # ---- api --------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify(ok=True, version=APP_VERSION)

    @app.get("/api/items")
    def items_list():
        c = conn()
        try:
            return jsonify(items=dbmod.list_items(c))
        finally:
            c.close()

    @app.post("/api/items")
    def items_add():
        data = _body()
        if data is None:
            return jsonify(error="expected a JSON object"), 400
        text = _str(data, "text")
        if not text:
            return jsonify(error="text required"), 400
        c = conn()
        try:
            return jsonify(item=dbmod.add_item(c, text)), 201
        finally:
            c.close()

    @app.put("/api/items/<int:item_id>")
    def items_update(item_id: int):
        data = _body()
        if data is None:
            return jsonify(error="expected a JSON object"), 400
        fields: dict = {}
        if "text" in data:
            text = _str(data, "text")
            if not text:
                return jsonify(error="text must be a non-empty string"), 400
            fields["text"] = text
        if "done" in data:
            done = _bool(data, "done")
            if done is None:
                return jsonify(error="done must be a boolean"), 400
            fields["done"] = done
        c = conn()
        try:
            item = dbmod.set_item(c, item_id, fields)
        finally:
            c.close()
        return (jsonify(item=item), 200) if item else (jsonify(error="not found"), 404)

    @app.delete("/api/items/<int:item_id>")
    def items_delete(item_id: int):
        c = conn()
        try:
            ok = dbmod.delete_item(c, item_id)
        finally:
            c.close()
        return (jsonify(ok=True), 200) if ok else (jsonify(error="not found"), 404)

    return app
