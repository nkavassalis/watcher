"""Browser tier (Playwright + headless Chromium) — the gate for any UI change.

Boots the REAL app as a subprocess against a throwaway DB and drives a real
browser: computed styles, element geometry, console errors, dialogs, gestures.

Run:  .venv/bin/python -m pytest -m ui
Auto-skips when Playwright/Chromium is missing (exit 0 + SKIP), so CI-less boxes
never fail on it — but a REAL project must have it green before a push.

House rules baked in below — do not remove:
  * assert the port is free instead of assuming (live services share this box)
  * wait on rendered content, not hash changes (hashchange renders one task later)
  * wait for hidden overlays with state="hidden", never a [hidden] selector
  * stub every third-party CDN global; tests must never fetch a CDN
  * assert page.errors == [] on every screen
  * assert no horizontal overflow at 390px (mobile is first-class, and rigid)
  * screenshots go to tests/screenshots/ (gitignored — they can hold live data)
"""
import socket
import subprocess
import sys
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


def _port_free():
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", PORT)) != 0


@pytest.fixture()
def server(tmp_path):
    assert _port_free(), f"port {PORT} is busy — stop the other service, don't kill it"
    SHOTS.mkdir(exist_ok=True)
    proc = subprocess.Popen(                       # TODO(project): your entrypoint + db/config args
        [sys.executable, "run.py", "--db", str(tmp_path / "ui.sqlite"),
         "--host", "127.0.0.1", "--port", str(PORT)],
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
    yield BASE
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def page(server, request):
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

def test_no_console_errors_and_no_h_scroll_on_mobile(page, server):
    page.goto(server + "/", wait_until="networkidle")
    page.set_viewport_size(MOBILE)
    page.wait_for_timeout(300)
    assert page.console_messages == [], page.console_messages
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390, f"horizontal overflow at 390px: {width}"


def test_rigid_shell_page_never_scrolls(page, server):
    """body must not scroll; exactly one scroller owns the overflow (house rule)."""
    page.goto(server + "/", wait_until="networkidle")
    page.set_viewport_size(MOBILE)
    page.wait_for_timeout(300)
    assert page.evaluate("document.body.scrollHeight") <= page.evaluate("window.innerHeight") + 1
