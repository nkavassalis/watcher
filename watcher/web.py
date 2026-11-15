"""Watcher — Flask app factory. Thin routes, no SQL, JSON errors always."""
from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

from . import config as configmod
from . import db as dbmod
from . import net
from .scheduler import Scheduler, check_once

APP_VERSION = "0.1.0"

_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app(db_path: str, config_path: str | None = None,
               cfg: dict | None = None, start_scheduler: bool = True) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["CONFIG_PATH"] = config_path or "config.json"
    app.config["CFG"] = cfg or configmod.load(config_path)
    net.configure(app.config["CFG"])

    def conn() -> sqlite3.Connection:
        # one connection per request/thread; never share across threads
        return dbmod.connect(app.config["DB_PATH"])

    dbmod.init_db(conn())

    sched: Scheduler | None = None
    if start_scheduler:
        sched = Scheduler(lambda: app.config["CFG"], db_path)
        sched.start()
    app.config["SCHEDULER"] = sched

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

    def _int(data: dict, key: str, lo: int, hi: int) -> int | None:
        v = data.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        return v if lo <= v <= hi else None

    def _url(data: dict, key: str) -> str | None:
        u = _str(data, key, maxlen=2000)
        if not u or not _URL_RE.match(u) or not urlparse(u).netloc:
            return None
        return u

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

    @app.get("/api/watches")
    def watches_list():
        c = conn()
        try:
            out = []
            for w in dbmod.list_watches(c):
                w["spec"] = json.loads(w["spec_json"]) if w["spec_json"] else None
                w.pop("spec_json", None)
                out.append(w)
            return jsonify(watches=out,
                           default_interval_minutes=
                           app.config["CFG"]["scheduler"]["default_interval_minutes"])
        finally:
            c.close()

    @app.post("/api/watches")
    def watches_add():
        data = _body()
        if data is None:
            return jsonify(error="expected a JSON object"), 400
        url = _url(data, "url")
        if not url:
            return jsonify(error="url must be a valid http(s) URL"), 400
        intent = _str(data, "intent")
        if not intent:
            return jsonify(error="intent required (what change do you want to hear about?)"), 400
        sched_cfg = app.config["CFG"]["scheduler"]
        interval = data.get("interval_minutes", sched_cfg["default_interval_minutes"])
        interval = _int({"i": interval}, "i", sched_cfg["min_interval_minutes"], 525600)
        if interval is None:
            return jsonify(error=f"interval_minutes must be an integer >= "
                                 f"{sched_cfg['min_interval_minutes']}"), 400
        notify_email = 1
        if "notify_email" in data:
            ne = _bool(data, "notify_email")
            if ne is None:
                return jsonify(error="notify_email must be a boolean"), 400
            notify_email = ne
        c = conn()
        try:
            return jsonify(watch=dbmod.create_watch(c, url, intent, interval,
                                                    notify_email)), 201
        finally:
            c.close()

    @app.put("/api/watches/<int:watch_id>")
    def watches_update(watch_id: int):
        data = _body()
        if data is None:
            return jsonify(error="expected a JSON object"), 400
        fields: dict = {}
        if "url" in data:
            url = _url(data, "url")
            if not url:
                return jsonify(error="url must be a valid http(s) URL"), 400
            fields["url"] = url
        if "intent" in data:
            intent = _str(data, "intent")
            if not intent:
                return jsonify(error="intent must be a non-empty string"), 400
            fields["intent"] = intent
        if "interval_minutes" in data:
            sched_cfg = app.config["CFG"]["scheduler"]
            iv = _int(data, "interval_minutes", sched_cfg["min_interval_minutes"], 525600)
            if iv is None:
                return jsonify(error="interval_minutes must be an integer >= "
                                     f"{sched_cfg['min_interval_minutes']}"), 400
            fields["interval_minutes"] = iv
        if "notify_email" in data:
            ne = _bool(data, "notify_email")
            if ne is None:
                return jsonify(error="notify_email must be a boolean"), 400
            fields["notify_email"] = ne
        if "status" in data:
            st = _str(data, "status")
            if st not in ("ready", "paused"):
                return jsonify(error="status must be 'ready' or 'paused'"), 400
            fields["status"] = st
        c = conn()
        try:
            if not dbmod.get_watch(c, watch_id):
                return jsonify(error="not found"), 404
            watch = dbmod.update_watch(c, watch_id, fields)
            if "url" in fields or "intent" in fields:
                # the spec was fitted to the old page/intent: re-onboard
                watch = dbmod.set_spec(c, watch_id, None, "pending", None)
                dbmod.schedule_next(c, watch_id, 0)
            elif fields.get("status") == "ready":
                dbmod.schedule_next(c, watch_id, 0)   # resume: check now
            return jsonify(watch=watch), 200
        finally:
            c.close()

    @app.delete("/api/watches/<int:watch_id>")
    def watches_delete(watch_id: int):
        c = conn()
        try:
            ok = dbmod.delete_watch(c, watch_id)
        finally:
            c.close()
        return (jsonify(ok=True), 200) if ok else (jsonify(error="not found"), 404)

    @app.post("/api/watches/<int:watch_id>/check")
    def watches_check(watch_id: int):
        c = conn()
        try:
            watch = dbmod.get_watch(c, watch_id)
            if not watch:
                return jsonify(error="not found"), 404
            if watch["status"] != "ready":
                return jsonify(error=f"watch is {watch['status']}; only ready "
                                     "watches can be checked"), 409
            updated = check_once(app.config["CFG"], c, watch)
            return jsonify(watch=updated), 200
        finally:
            c.close()

    @app.post("/api/watches/<int:watch_id>/reonboard")
    def watches_reonboard(watch_id: int):
        c = conn()
        try:
            if not dbmod.get_watch(c, watch_id):
                return jsonify(error="not found"), 404
            watch = dbmod.set_spec(c, watch_id, None, "pending", None)
            dbmod.schedule_next(c, watch_id, 0)
            return jsonify(watch=watch), 200
        finally:
            c.close()

    @app.get("/api/watches/<int:watch_id>/checks")
    def checks_list(watch_id: int):
        c = conn()
        try:
            if not dbmod.get_watch(c, watch_id):
                return jsonify(error="not found"), 404
            return jsonify(checks=dbmod.list_checks(c, watch_id))
        finally:
            c.close()

    @app.get("/api/settings")
    def settings_get():
        return jsonify(settings=configmod.masked_config(app.config["CFG"]))

    @app.put("/api/settings")
    def settings_put():
        data = _body()
        if data is None:
            return jsonify(error="expected a JSON object"), 400
        cfg = app.config["CFG"]
        s = data.get("settings")
        if not isinstance(s, dict):
            return jsonify(error="settings object required"), 400
        llm_in = s.get("llm")
        if isinstance(llm_in, dict):
            if "base_url" in llm_in:
                u = _url(llm_in, "base_url")
                if not u:
                    return jsonify(error="llm.base_url must be a valid http(s) URL"), 400
                cfg["llm"]["base_url"] = u
            if "model" in llm_in:
                m = _str(llm_in, "model", maxlen=200)
                cfg["llm"]["model"] = m if m is not None else ""
        em_in = s.get("email")
        if isinstance(em_in, dict):
            if "enabled" in em_in:
                b = _bool(em_in, "enabled")
                if b is None:
                    return jsonify(error="email.enabled must be a boolean"), 400
                cfg["email"]["enabled"] = bool(b)
            for key in ("to", "from_addr", "from_name"):
                if key in em_in:
                    v = _str(em_in, key, maxlen=300)
                    if key == "to" and v and not _EMAIL_RE.match(v):
                        return jsonify(error="email.to must be an email address"), 400
                    cfg["email"][key] = v or ""
        sch_in = s.get("scheduler")
        if isinstance(sch_in, dict) and "default_interval_minutes" in sch_in:
            iv = _int(sch_in, "default_interval_minutes",
                      cfg["scheduler"]["min_interval_minutes"], 525600)
            if iv is None:
                return jsonify(error="default_interval_minutes out of range"), 400
            cfg["scheduler"]["default_interval_minutes"] = iv
        # api_key / gmail_dir are deliberately NOT editable from the UI;
        # they live in config.json / the environment only.
        try:
            configmod.save(app.config["CONFIG_PATH"], cfg)
        except OSError:
            pass   # read-only config file must not break the running app
        net.configure(cfg)
        return jsonify(settings=configmod.masked_config(cfg))

    return app
