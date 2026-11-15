"""Static contract between template, JS and CSS — hermetic, no browser.

Catches "the button exists but does nothing" and "the overlay is visible on
load" before a human does. If this fails, WIRE THE THING; never weaken it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = next(p for p in ROOT.iterdir() if (p / "web.py").exists())

TPL = PKG / "templates" / "index.html"
JS = PKG / "static" / "app.js"
CSS = PKG / "static" / "style.css"

DYNAMIC: set[str] = set()          # ids created at runtime — keep this honest


@pytest.fixture(scope="module")
def html():
    return TPL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js():
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css():
    return CSS.read_text(encoding="utf-8")


def test_hidden_display_rule_is_load_bearing(css):
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), \
        "[hidden]{display:none !important} must exist"


def test_every_js_id_exists_in_template(html, js):
    wanted = set(re.findall(r"""\$\(\s*['"]#([\w-]+)['"]""", js)) | set(
        re.findall(r"""getElementById\(\s*['"]([\w-]+)['"]""", js))
    present = set(re.findall(r'id="([\w-]+)"', html)) | DYNAMIC
    missing = wanted - present
    assert not missing, f"app.js reaches for ids the template never defines: {sorted(missing)}"


def test_every_data_act_has_a_handler(js):
    emitted = set(re.findall(r'data-act="([\w-]+)"', js)) | set(
        re.findall(r"""dataset\.act\s*=\s*['"]([\w-]+)['"]""", js))
    handled = set(re.findall(r"""case\s+['"]([\w-]+)['"]""", js)) | set(
        re.findall(r"""act\s*===?\s*['"]([\w-]+)['"]""", js))
    orphans = emitted - handled
    assert not orphans, f"data-act with no handler (dead button): {sorted(orphans)}"


def test_overlays_ship_hidden(html):
    for m in re.finditer(r'<(section|div)\b[^>]*class="[^"]*\boverlay\b[^"]*"[^>]*>', html):
        assert "hidden" in m.group(0), f"overlay must ship hidden: {m.group(0)[:120]}"


def test_innerhtml_only_uses_escaped_values(js):
    for m in re.finditer(r"\.innerHTML\s*=\s*([^;]{0,200})", js):
        expr = m.group(1)
        assert "esc(" in expr, f"innerHTML of unescaped value: {expr}"
