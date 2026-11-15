"""API contract + input hygiene. /api/* answers JSON, never an HTML 500.
The scheduler thread is off in this tier; onboarding/checking is tested in
test_scheduler/test_onboard, so routes here are exercised with fetch stubbed.
"""
import json

from watcher import db as dbmod
from watcher import net
from watcher.net import FetchResult


def _add(client, url="https://x.example/p", intent="back in stock", **kw):
    return client.post("/api/watches", json={"url": url, "intent": intent, **kw})


# ---- watches CRUD -------------------------------------------------------------

def test_add_defaults(client, app):
    r = _add(client, intent="  back   in   stock  ")
    assert r.status_code == 201
    w = r.get_json()["watch"]
    assert w["intent"] == "back in stock"                # whitespace-collapsed
    assert w["name"] is None                             # unnamed: onboarding will guess
    assert w["status"] == "pending" and w["interval_minutes"] == 1440
    assert w["notify_email"] == 1


def test_add_and_update_name(client):
    w = _add(client, name="  Double X film  ").get_json()["watch"]
    assert w["name"] == "Double X film"
    r = client.put(f"/api/watches/{w['id']}", json={"name": "B&W film restock"})
    up = r.get_json()["watch"]
    assert up["name"] == "B&W film restock" and up["name_is_auto"] == 0
    assert up["status"] == "pending"                     # name edit does NOT re-fit
    # clearing the name hands it back to the auto-guesser
    r = client.put(f"/api/watches/{w['id']}", json={"name": "   "})
    up = r.get_json()["watch"]
    assert up["name"] is None and up["name_is_auto"] == 1
    assert client.put(f"/api/watches/{w['id']}", json={"name": 42}).status_code == 400


def test_add_validation(client):
    assert _add(client, url="not a url").status_code == 400
    assert _add(client, url="ftp://x.example/p").status_code == 400
    assert _add(client, url="https://x.example").status_code == 201   # bare host is fine
    assert _add(client, intent="").status_code == 400
    assert _add(client, intent="   ").status_code == 400
    assert _add(client, interval_minutes=1).status_code == 400        # below floor
    assert _add(client, interval_minutes="daily").status_code == 400
    assert _add(client, interval_minutes=True).status_code == 400
    assert _add(client, notify_email="yes").status_code == 400
    assert client.post("/api/watches", data="junk").status_code == 400
    assert client.post("/api/watches", json=[1]).status_code == 400


def test_list_exposes_parsed_spec(client, app):
    w = _add(client).get_json()["watch"]
    c = dbmod.connect(app.config["DB_PATH"])
    dbmod.set_spec(c, w["id"], json.dumps({"mode": "hash"}), "ready")
    c.close()
    rows = client.get("/api/watches").get_json()["watches"]
    assert rows[0]["spec"] == {"mode": "hash"} and "spec_json" not in rows[0]


def test_update_fields_and_reonboard_trigger(client, app):
    w = _add(client).get_json()["watch"]
    r = client.put(f"/api/watches/{w['id']}", json={"interval_minutes": 60})
    assert r.get_json()["watch"]["interval_minutes"] == 60
    r = client.put(f"/api/watches/{w['id']}", json={"status": "paused"})
    assert r.get_json()["watch"]["status"] == "paused"
    r = client.put(f"/api/watches/{w['id']}", json={"status": "bogus"})
    assert r.status_code == 400
    r = client.put(f"/api/watches/{w['id']}", json={"interval_minutes": 0})
    assert r.status_code == 400
    # editing the url re-fits the detector: back to pending
    c = dbmod.connect(app.config["DB_PATH"])
    dbmod.set_spec(c, w["id"], json.dumps({"mode": "hash"}), "ready")
    c.close()
    r = client.put(f"/api/watches/{w['id']}", json={"url": "https://y.example/q"})
    up = r.get_json()["watch"]
    assert up["status"] == "pending" and up["spec_json"] is None
    assert client.put("/api/watches/999", json={"interval_minutes": 60}).status_code == 404


def test_delete(client):
    w = _add(client).get_json()["watch"]
    assert client.delete(f"/api/watches/{w['id']}").status_code == 200
    assert client.delete(f"/api/watches/{w['id']}").status_code == 404


# ---- check / reonboard / history ---------------------------------------------

def test_check_requires_ready(client, monkeypatch):
    w = _add(client).get_json()["watch"]
    r = client.post(f"/api/watches/{w['id']}/check")
    assert r.status_code == 409                       # still pending
    monkeypatch.setattr(net, "fetch",
                        lambda url: FetchResult(url=url, status="ok", code=200,
                                                text="<p>Out of stock</p>", rung=0))
    c = dbmod.connect(client.application.config["DB_PATH"])
    dbmod.set_spec(c, w["id"], json.dumps(
        {"mode": "condition", "present": ["In stock"], "absent": ["Out of stock"],
         "min_present": 1}), "ready")
    c.close()
    r = client.post(f"/api/watches/{w['id']}/check")
    assert r.status_code == 200
    assert r.get_json()["watch"]["last_status"] == "ok"
    assert client.post("/api/watches/999/check").status_code == 404


def test_reonboard_resets_to_pending(client):
    w = _add(client).get_json()["watch"]
    c = dbmod.connect(client.application.config["DB_PATH"])
    dbmod.set_spec(c, w["id"], json.dumps({"mode": "hash"}), "ready")
    c.close()
    r = client.post(f"/api/watches/{w['id']}/reonboard")
    assert r.status_code == 200 and r.get_json()["watch"]["status"] == "pending"
    assert client.post("/api/watches/999/reonboard").status_code == 404


def test_checks_history(client):
    w = _add(client).get_json()["watch"]
    c = dbmod.connect(client.application.config["DB_PATH"])
    dbmod.record_check(c, w["id"], "ok", rung=0, detail="fine")
    c.close()
    rows = client.get(f"/api/watches/{w['id']}/checks").get_json()["checks"]
    assert rows[0]["status"] == "ok"
    assert client.get("/api/watches/999/checks").status_code == 404


# ---- settings -------------------------------------------------------------

def test_settings_masking_and_update(client, app):
    app.config["CFG"]["llm"]["api_key"] = "super-secret-key"
    s = client.get("/api/settings").get_json()["settings"]
    assert s["llm"]["api_key"] == "***"
    assert "super-secret-key" not in json.dumps(s)

    r = client.put("/api/settings", json={"settings": {
        "llm": {"base_url": "http://llm.example:8000/v1", "model": "m1"},
        "email": {"enabled": True, "to": "a@b.co", "from_addr": "w@b.co"},
        "scheduler": {"default_interval_minutes": 60}}})
    assert r.status_code == 200
    cfg = app.config["CFG"]
    assert cfg["llm"]["base_url"] == "http://llm.example:8000/v1"
    assert cfg["email"]["enabled"] is True and cfg["scheduler"]["default_interval_minutes"] == 60

    assert client.put("/api/settings", json={"settings": {
        "email": {"to": "not-an-email"}}}).status_code == 400
    assert client.put("/api/settings", json={"settings": {
        "llm": {"base_url": "not a url"}}}).status_code == 400
    assert client.put("/api/settings", json={"settings": {
        "email": {"enabled": "on"}}}).status_code == 400
    assert client.put("/api/settings", json={}).status_code == 400
    # api_key is not UI-editable
    client.put("/api/settings", json={"settings": {"llm": {"api_key": "x"}}})
    assert app.config["CFG"]["llm"]["api_key"] == "super-secret-key"


def test_settings_persist_to_config_file(tmp_path):
    from conftest import inert_cfg

    from watcher.web import create_app
    cfg_path = tmp_path / "cfg.json"
    app = create_app(str(tmp_path / "a.sqlite"), config_path=str(cfg_path),
                     cfg=inert_cfg(), start_scheduler=False)
    c = app.test_client()
    c.put("/api/settings", json={"settings": {
        "llm": {"base_url": "http://llm.example:9/v1", "model": "m"}}})
    saved = json.loads(cfg_path.read_text())
    assert saved["llm"]["base_url"] == "http://llm.example:9/v1"
    assert "api_key" not in saved.get("llm", {}) or saved["llm"]["api_key"] in ("", None)


def test_settings_put_never_writes_repo_config(client):
    """Tripwire (scar 2026-11-15): a hermetic settings PUT once clobbered the
    repo's real ./config.json — the file the live instance boots from. If a
    future fixture or route starts writing there, this fails."""
    from pathlib import Path
    root_cfg = Path.cwd() / "config.json"
    before = root_cfg.read_bytes() if root_cfg.exists() else None
    client.put("/api/settings", json={"settings": {
        "llm": {"base_url": "http://clobber-me.example/v1", "model": "x"},
        "email": {"enabled": True, "to": "clobber@example.com"}}})
    after = root_cfg.read_bytes() if root_cfg.exists() else None
    assert before == after, "hermetic test wrote the repo's live config.json"


def test_test_mode_blocks_implicit_config_write(tmp_path, monkeypatch):
    """The guard mechanism itself: under WATCHER_TEST_MODE the implicit
    ./config.json is never written, but an EXPLICIT config_path still is
    (deployments and deliberate tests depend on that)."""
    from conftest import inert_cfg

    from watcher.web import create_app
    monkeypatch.chdir(tmp_path)                    # ./config.json resolves in tmp
    app = create_app(str(tmp_path / "b.sqlite"), cfg=inert_cfg(),
                     start_scheduler=False)
    app.test_client().put("/api/settings", json={"settings": {"llm": {"model": "m"}}})
    assert not (tmp_path / "config.json").exists()

    cfgp = tmp_path / "explicit.json"
    app2 = create_app(str(tmp_path / "d.sqlite"), config_path=str(cfgp),
                      cfg=inert_cfg(), start_scheduler=False)
    app2.test_client().put("/api/settings", json={"settings": {"llm": {"model": "m2"}}})
    assert json.loads(cfgp.read_text())["llm"]["model"] == "m2"


# ---- error shape -------------------------------------------------------------

def test_errors_under_api_are_json(client):
    for r in (client.post("/api/watches", data="x"),
              client.put("/api/watches/nope", json={}),
              client.delete("/api/watches/999999"),
              client.get("/api/watches/999/checks")):
        assert r.status_code >= 400
        assert r.is_json and "error" in r.get_json()
