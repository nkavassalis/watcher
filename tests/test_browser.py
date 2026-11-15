"""Rungs 3-4 with fake page objects and a scripted LLM.

The gate is the point: the model may only choose among actions the code
permits, and a click only executes when the text verifiably exists in the
snapshot we collected. Every anomaly resolves to give_up, never to autonomy.
"""
import re

import pytest

from watcher import browser, llm


class FakeEl:
    def __init__(self, name, visible=True):
        self.name, self.visible = name, visible

    def is_visible(self):
        return self.visible

    def click(self, timeout=None):
        CLICKS.append(self.name)


CLICKS: list[str] = []


class FakeLocator:
    def __init__(self, els):
        self._els = els

    @property
    def first(self):
        if not self._els:
            raise LookupError("no match")
        return self._els[0]

    def all_inner_texts(self):
        return [e.name for e in self._els]


class FakePage:
    """Implements just enough of the Playwright page surface for rungs 3-4."""

    def __init__(self, controls, title="Just a moment...", body="verify you are human"):
        self.controls = controls
        self._title = title
        self._body = body

    def flip_ok(self):
        self._title = "Reflx Lab Double X"
        self._body = "In stock — add to cart"

    def title(self):
        return self._title

    def inner_text(self, sel):
        return self._body

    def content(self):
        return ("<html><body>" + self._body + "</body></html>") * 100

    def get_by_role(self, role, name=None):
        els = []
        for c in self.controls:
            if isinstance(name, re.Pattern):
                if name.search(c):
                    els.append(FakeEl(c))
            elif name == c:
                els.append(FakeEl(c))
        return FakeLocator(els)

    def locator(self, sel):
        return FakeLocator([FakeEl(c) for c in self.controls])


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    CLICKS.clear()
    monkeypatch.setattr(browser.time, "sleep", lambda s: None)


# ---- rung 3: rules -------------------------------------------------------------

def test_modal_rules_click_known_buttons():
    page = FakePage(["Accept all cookies", "Buy now"])
    assert browser.dismiss_modals(page) == 1
    assert CLICKS == ["Accept all cookies"]


def test_modal_rules_cap_at_three_clicks():
    page = FakePage(["Accept cookies", "I agree", "Got it", "Continue as guest",
                     "No thanks"])
    assert browser.dismiss_modals(page) <= browser.MAX_MODAL_CLICKS
    assert len(CLICKS) <= 3


def test_modal_rules_ignore_unrelated_buttons():
    page = FakePage(["Buy now", "Login"])
    assert browser.dismiss_modals(page) == 0
    assert CLICKS == []


# ---- rung 4: gated rescue -------------------------------------------------------

def _script(monkeypatch, actions):
    calls = {"n": 0}

    def fake_chat_json(cfg, messages, **kw):
        a = actions[calls["n"]] if calls["n"] < len(actions) else {"action": "give_up"}
        calls["n"] += 1
        return a

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return calls


CFG = {"fetch": {"rescue_max_steps": 4, "rescue_wall_seconds": 45},
       "llm": {}}


def test_rescue_clicks_only_snapshot_texts_and_succeeds(monkeypatch):
    page = FakePage(["Accept cookies"])
    actions = [{"action": "click", "text": "Accept cookies"}]

    def fake_chat_json(cfg, messages, **kw):
        return actions.pop(0) if actions else {"action": "give_up"}

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)

    def click(self, timeout=None):          # the click clears the interstitial
        CLICKS.append(self.name)
        page.flip_ok()

    monkeypatch.setattr(FakeEl, "click", click)
    assert browser.rescue(page, CFG) == "ok"
    assert CLICKS == ["Accept cookies"]


def test_rescue_refuses_hallucinated_text(monkeypatch):
    page = FakePage(["Accept cookies"])
    _script(monkeypatch, [{"action": "click", "text": "Sign in with Google"}])
    assert browser.rescue(page, CFG) == "gave_up"
    assert CLICKS == []                       # never a click on invented text


def test_rescue_refuses_nonstring_text(monkeypatch):
    page = FakePage(["Accept cookies"])
    _script(monkeypatch, [{"action": "click", "text": 42}])
    assert browser.rescue(page, CFG) == "gave_up"
    assert CLICKS == []


def test_rescue_unknown_action_gives_up(monkeypatch):
    page = FakePage(["Accept cookies"])
    _script(monkeypatch, [{"action": "type", "text": "password", "target": "user"}])
    assert browser.rescue(page, CFG) == "gave_up"
    assert CLICKS == []


def test_rescue_llm_error_gives_up(monkeypatch):
    page = FakePage(["Accept cookies"])

    def boom(cfg, messages, **kw):
        raise llm.LLMError("endpoint down")

    monkeypatch.setattr(llm, "chat_json", boom)
    assert browser.rescue(page, CFG) == "gave_up"


def test_rescue_explicit_give_up(monkeypatch):
    page = FakePage(["Accept cookies"])
    _script(monkeypatch, [{"action": "give_up"}])
    assert browser.rescue(page, CFG) == "gave_up"


def test_rescue_respects_step_cap(monkeypatch):
    page = FakePage(["Accept cookies"])
    calls = _script(monkeypatch, [{"action": "wait", "seconds": 0.1}] * 20)
    assert browser.rescue(page, CFG) == "gave_up"
    assert calls["n"] == CFG["fetch"]["rescue_max_steps"]     # hard cap, no runaway


def test_rescue_invisible_element_gives_up(monkeypatch):
    page = FakePage(["Accept cookies"])

    class Hidden(FakeEl):
        def is_visible(self):
            return False

    page.get_by_role = lambda role, name=None: FakeLocator(
        [Hidden("Accept cookies")] if name == "Accept cookies" else [])
    _script(monkeypatch, [{"action": "click", "text": "Accept cookies"}])
    assert browser.rescue(page, CFG) == "gave_up"


def test_snapshot_collects_controls_and_budgets_excerpt():
    page = FakePage(["A" * 300, "Buy"], body="x" * 5000)
    snap = browser.snapshot(page, excerpt_chars=100)
    assert len(snap["excerpt"]) == 100
    assert "Buy" in snap["controls"] and len(snap["controls"]) <= 40


def test_stuck_detection():
    assert browser._looks_stuck("Just a moment...", "", "x" * 9999)
    assert browser._looks_stuck("ok", "verify you are human", "x" * 9999)
    assert browser._looks_stuck("ok", "fine", "tiny")          # suspiciously small DOM
    assert not browser._looks_stuck("Product", "In stock", "x" * 9999)
