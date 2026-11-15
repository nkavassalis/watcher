"""Browser tier (Playwright + headless Chromium) — the gate for any UI change.

Boots the REAL app as a subprocess against a throwaway DB and drives a real
browser: computed styles, element geometry, console errors, dialogs.
Onboarding is exercised end to end against a LOCAL stub target site and a
LOCAL stub LLM — the hermetic philosophy holds at this tier too: no third
party is ever contacted.

Run:  .venv/bin/python -m pytest -m ui
Auto-skips when Playwright/Chromium is missing (exit 0 + SKIP), so CI-less boxes
never fail on it — but a REAL project must have it green before a push.

House rules baked in below — do not remove:
  * assert the port is free instead of assuming (live services share this box)
  * wait on rendered content, not hash changes
  * wait for hidden overlays with state="hidden", never a [hidden] selector
  * stub every third-party CDN global; tests must never fetch a CDN
  * assert page.errors == [] on every screen
  * assert no horizontal overflow at 390px (mobile is first-class, and rigid)
  * screenshots go to tests/screenshots/ (gitignored — they can hold live data)
"""
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

pytestmark = [pytest.mark.ui, pytest.mark.skipif(not HAVE_PW, reason="playwright not installed")]

ROOT = Path(__file__).resolve().parents[1]
SHOTS = Path(__file__).resolve().parent / "screenshots"
PORT = 8177
BASE = f"http://127.0.0.1:{PORT}"
DESKTOP = {"width": 1280, "height": 800}
MOBILE = {"width": 390, "height": 844}
WAIT_MS = 6000                   # 3s flakes under tier load; this is the house convention


def _port_free(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- the stub world: a shop and an LLM, both local --------------------------------

def make_target():
    """A tiny shop page whose stock state the test can flip."""
    from flask import Flask, request
    app = Flask("stub-target")
    state = {"stock": False}

    @app.route("/product")
    def product():
        if state["stock"]:
            avail, line = "InStock", "In stock — add to cart"
        else:
            avail, line = "OutOfStock", "Out of stock"
        return f"""<!doctype html><html><head><title>Reflx Lab Double X 35mm</title>
        <script type="application/ld+json">{{"@type":"Offer",
        "availability":"http://schema.org/{avail}"}}</script></head>
        <body><h1>Reflx Lab Double X</h1><p>{line}</p>
        <button>Accept all cookies</button></body></html>"""

    @app.route("/admin")
    def admin():
        state["stock"] = request.args.get("stock") == "1"
        return "ok"

    return app, state


def make_llm():
    """An OpenAI-compatible stub: echoes a stock spec whose current_met matches
    what the prompt's visible text says — so the cross-check passes honestly."""
    from flask import Flask, request
    app = Flask("stub-llm")

    @app.route("/v1/models")
    def models():
        return json.dumps({"data": [{"id": "stub-model"}]})

    @app.route("/v1/chat/completions", methods=["POST"])
    def completions():
        prompt = request.get_json()["messages"][-1]["content"]
        met = "Out of stock" not in prompt
        spec = {"mode": "condition", "present": ["In stock"],
                "absent": ["Out of stock"], "min_present": 1,
                "current_met": met, "notes": "stock check",
                "name": "Stub stock watch"}
        return json.dumps({"choices": [{"message": {"content": json.dumps(spec)}}]})

    return app


def _serve(app, port):
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture()
def world(tmp_path):
    """Stub target + stub LLM in-process; the app under test as a subprocess."""
    assert _port_free(PORT), f"port {PORT} is busy — stop the other service, don't kill it"
    target_port, llm_port = _free_port(), _free_port()
    target_app, stock_state = make_target()
    llm_app = make_llm()
    srv1 = _serve(target_app, target_port)
    srv2 = _serve(llm_app, llm_port)
    SHOTS.mkdir(exist_ok=True)

    cfg = {
        "llm": {"base_url": f"http://127.0.0.1:{llm_port}/v1", "model": ""},
        "email": {"enabled": False},
        "fetch": {"challenge_wait_seconds": 0, "browser_enabled": False},
        "scheduler": {"tick_seconds": 1},
    }
    cfg_path = tmp_path / "ui-config.json"
    cfg_path.write_text(json.dumps(cfg))
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--db", str(tmp_path / "ui.sqlite"),
         "--host", "127.0.0.1", "--port", str(PORT), "--config", str(cfg_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{BASE}/api/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("server did not come up: " + (proc.stdout.read().decode()[-2000:]))
    yield {"base": BASE, "target": f"http://127.0.0.1:{target_port}",
           "stock": stock_state, "cfg_path": cfg_path}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    srv1.shutdown()
    srv2.shutdown()


@pytest.fixture()
def page(world, request):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=request.param if hasattr(request, "param") else DESKTOP)
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.console_messages = errors               # attach for assertions
        yield pg
        browser.close()


# --- the two assertions every page gets -------------------------------------------------

def test_no_console_errors_and_no_h_scroll_on_mobile(page, world):
    page.goto(world["base"] + "/", wait_until="networkidle")
    page.set_viewport_size(MOBILE)
    page.wait_for_timeout(300)
    assert page.console_messages == [], page.console_messages
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390, f"horizontal overflow at 390px: {width}"


def test_rigid_shell_page_never_scrolls(page, world):
    """body must not scroll; exactly one scroller owns the overflow (house rule)."""
    page.goto(world["base"] + "/", wait_until="networkidle")
    page.set_viewport_size(MOBILE)
    page.wait_for_timeout(300)
    assert page.evaluate("document.body.scrollHeight") <= page.evaluate("window.innerHeight") + 1


# --- the app itself --------------------------------------------------------------------

def _add_watch(page, url, intent, interval="1440", name=""):
    if name:
        page.fill("#new-name", name)
    page.fill("#new-url", url)
    page.fill("#new-intent", intent)
    page.select_option("#new-interval", interval)
    page.click("#add-btn")


def test_empty_state_then_add_onboard_and_trigger(page, world):
    page.goto(world["base"] + "/", wait_until="networkidle")
    assert page.is_visible("#empty")

    _add_watch(page, world["target"] + "/product", "back in stock", name="Double X film")
    # the row appears as pending, then the scheduler onboards it via the stub LLM
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    assert page.console_messages == [], page.console_messages
    row = page.locator(".watch.ready").first
    # a user-typed name survives onboarding (the LLM's guess is ignored)
    assert "Double X film" in row.locator(".w-title").inner_text()
    assert "Stub stock watch" not in row.inner_text()
    assert "/product" in row.locator(".w-url").inner_text()
    assert "WATCHING FOR:" in row.inner_text().upper()   # the intent is labelled, not bare
    assert "back in stock" in row.inner_text()
    assert "Check daily" in row.inner_text()     # the frequency is labelled too
    assert "stock check" in row.inner_text()     # the detector note from the spec
    assert page.locator("#empty").is_hidden()

    # out of stock still: a manual check must NOT trigger
    row.locator('[data-act="check"]').click()
    page.wait_for_selector(".w-meta:has-text('still shows: Out of stock')", timeout=WAIT_MS)
    assert page.locator(".watch.triggered").count() == 0

    # restock, check now -> triggered
    urllib.request.urlopen(world["target"] + "/admin?stock=1", timeout=5)
    page.locator('[data-act="check"]').first.click()
    page.wait_for_selector(".watch.triggered", timeout=WAIT_MS)
    assert "▲" in page.locator(".watch .glyph").first.inner_text()

    # history shows the changed row
    page.locator('[data-act="history"]').first.click()
    page.wait_for_selector(".w-history .h-status.s-changed", timeout=WAIT_MS)
    page.screenshot(path=str(SHOTS / "triggered.png"))


def test_edit_watch_sheet(page, world):
    """Edit changes intent + interval; changing the intent re-fits the detector
    (watch goes pending, the scheduler re-onboards it via the stub LLM)."""
    page.goto(world["base"] + "/", wait_until="networkidle")
    _add_watch(page, world["target"] + "/product", "back in stock")
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    # unnamed watch: the onboarding LLM pass named it automatically
    assert "Stub stock watch" in page.locator(".w-title").first.inner_text()

    page.locator('[data-act="edit"]').first.click()
    page.wait_for_selector("#edit-overlay", state="visible")
    assert page.input_value("#edit-intent") == "back in stock"
    assert page.input_value("#edit-name") == "Stub stock watch"   # the auto name, editable
    page.fill("#edit-name", "Graination Double X")
    page.fill("#edit-intent", "price drops under 20")
    page.select_option("#edit-interval", "60")
    page.click("#save-edit")
    page.wait_for_selector("#edit-overlay", state="hidden", timeout=WAIT_MS)

    # saved + re-fitting: pending, then ready again with the new intent
    page.wait_for_selector(".w-intent:has-text('price drops under 20')", timeout=WAIT_MS)
    assert "Graination Double X" in page.locator(".w-title").first.inner_text()
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    assert "Check hourly" in page.locator(".watch").first.inner_text()
    assert page.console_messages == [], page.console_messages

    # re-fit regenerates the detector but never touches a user-set name
    page.locator('[data-act="edit"]').first.click()
    page.wait_for_selector("#edit-overlay", state="visible")
    page.click("#refit-edit")
    page.wait_for_selector("#toast:has-text('Re-fitting detector')", state="visible",
                           timeout=WAIT_MS)
    page.wait_for_selector("#edit-overlay", state="hidden", timeout=WAIT_MS)
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    assert "Graination Double X" in page.locator(".w-title").first.inner_text()
    assert "Stub stock watch" not in page.locator(".watch").first.inner_text()
    assert page.console_messages == [], page.console_messages


def test_edit_preserves_custom_interval(page, world):
    """An API-created watch with an off-preset interval (45 min) must survive
    an edit-sheet Save: the select gains the option instead of silently
    snapping to a preset."""
    body = json.dumps({"url": world["target"] + "/product",
                       "intent": "anything", "interval_minutes": 45,
                       "notify_email": False}).encode()
    req = urllib.request.Request(world["base"] + "/api/watches", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    urllib.request.urlopen(req, timeout=5)
    page.goto(world["base"] + "/", wait_until="networkidle")
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    assert "every 45 min" in page.locator(".w-meta").first.inner_text()
    page.locator('[data-act="edit"]').first.click()
    page.wait_for_selector("#edit-overlay", state="visible")
    assert page.input_value("#edit-interval") == "45"
    page.click("#save-edit")
    page.wait_for_selector("#edit-overlay", state="hidden", timeout=WAIT_MS)
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    assert "every 45 min" in page.locator(".w-meta").first.inner_text()


def test_pause_and_resume(page, world):
    page.goto(world["base"] + "/", wait_until="networkidle")
    _add_watch(page, world["target"] + "/product", "price drop")
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)
    page.locator('[data-act="pause"]').first.click()
    page.wait_for_selector(".watch.paused", timeout=WAIT_MS)
    page.locator('[data-act="resume"]').first.click()
    page.wait_for_selector(".watch.ready", timeout=WAIT_MS)


def test_settings_roundtrip(page, world):
    page.goto(world["base"] + "/", wait_until="networkidle")
    page.click("#settings-btn")
    page.wait_for_selector("#settings-overlay", state="visible")
    page.fill("#set-email-to", "someone@example.com")
    page.click("#save-settings")
    page.wait_for_selector("#settings-overlay", state="hidden", timeout=WAIT_MS)

    # persisted server-side AND in the config file the subprocess was given
    saved = json.loads(world["cfg_path"].read_text())
    assert saved["email"]["to"] == "someone@example.com"
    page.click("#settings-btn")
    page.wait_for_selector("#settings-overlay", state="visible")
    assert page.input_value("#set-email-to") == "someone@example.com"


def test_delete_watch(page, world):
    page.goto(world["base"] + "/", wait_until="networkidle")
    _add_watch(page, world["target"] + "/product", "anything")
    page.wait_for_selector(".watch", timeout=WAIT_MS)
    page.locator('[data-act="del"]').first.click()
    page.wait_for_selector("#confirm-overlay", state="visible")
    page.click("#confirm-yes")
    page.wait_for_selector("#empty", state="visible", timeout=WAIT_MS)
