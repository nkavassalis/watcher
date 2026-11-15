"""Home-screen (PWA) contract — hermetic, no browser.

These are the things that silently break Add-to-Home-Screen and are invisible in a
desktop browser test. If one fails, fix the asset; don't delete the assertion.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = next(p for p in ROOT.iterdir() if (p / "web.py").exists())
STATIC = PKG / "static"



def Image():
    """Pillow is a dev dependency; import lazily so the module still collects
    without it (the assertions then skip rather than error)."""
    pytest.importorskip("PIL", reason="pillow is in requirements-dev.txt")
    from PIL import Image as _Image
    return _Image


@pytest.fixture(scope="module")
def html():
    return (PKG / "templates" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def manifest():
    return json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))


def test_manifest_is_installable(manifest):
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest.get("short_name") and manifest.get("name")
    assert manifest.get("background_color"), "splash colour shows before CSS lands"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_manifest_icons_exist_and_are_opaque_squares(manifest):
    for entry in manifest["icons"]:
        name = entry["src"].rsplit("/", 1)[-1]
        path = STATIC / name
        assert path.exists(), f"manifest advertises {name} which does not exist"
        img = Image().open(path)
        assert img.size == tuple(int(x) for x in entry["sizes"].split("x"))
        assert img.mode == "RGB", f"{name} must be opaque (no alpha) — iOS composites it"


def test_apple_touch_icon_is_180_opaque_square():
    """180px, no alpha, no rounded corners: iOS masks the icon itself, so alpha or
    radius shows up as a dark halo or a double-rounded icon on the home screen."""
    path = STATIC / "apple-touch-icon.png"
    assert path.exists(), "Safari needs the PNG — do not delete it because SVG exists"
    img = Image().open(path)
    assert img.size == (180, 180)
    assert img.mode == "RGB"


def test_favicon_set_present(html):
    assert (STATIC / "favicon.svg").exists(), "SVG favicon is the source of truth"
    assert (STATIC / "favicon.png").exists(), "PNG fallback for browsers that skip SVG favicons"
    assert 'rel="icon" type="image/svg+xml"' in html
    assert 'rel="apple-touch-icon"' in html


def test_ios_head_tags_present(html):
    for tag in ('name="viewport"', "viewport-fit=cover",
                'name="apple-mobile-web-app-capable" content="yes"',
                'name="apple-mobile-web-app-status-bar-style"',
                'name="theme-color"',
                'rel="manifest"'):
        assert tag in html, f"missing home-screen head tag: {tag}"


def test_theme_is_applied_server_side(html):
    """House rule: if the app has a theme at all, it is rendered SERVER-side into
    <html data-theme=...> from the stored setting — client-side application means a
    white flash on every cold start in dark mode. (The starter ships without a theme
    setting; when you add one, this test starts enforcing it — that is intentional.)"""
    has_theme = "theme" in (PKG / "web.py").read_text(encoding="utf-8") + \
                (PKG / "static" / "app.js").read_text(encoding="utf-8")
    if not has_theme:
        pytest.skip("app has no theme setting yet — add data-theme when it does")
    assert re.search(r'<html[^>]*data-theme=', html), "data-theme must be rendered server-side"


def test_ios_standalone_css_scars(css=None):
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert re.search(r"height:\s*100vh;\s*height:\s*100dvh", css), \
        "100vh fallback must precede 100dvh"
    assert re.search(r"html[^{]*\{[^}]*background", css) or \
        re.search(r"html,\s*body\s*\{[^}]*background", css), \
        "html must paint --bg (iOS standalone shows the white canvas under a short 100dvh)"
    assert re.search(r"#bottombar\s*\{[^}]*position:\s*fixed", css), \
        "bottom bar is fixed to the viewport bottom (dvh undershoot scar)"
    assert "env(safe-area-inset-bottom)" in css and "env(safe-area-inset-top)" in css
