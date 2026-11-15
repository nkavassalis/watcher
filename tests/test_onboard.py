"""Onboarding: LLM spec drafting, the current_met cross-check, and outcomes.
The LLM is always scripted — the hermetic tier never touches a real endpoint.
"""
import json

from watcher import db as dbmod
from watcher import llm, net, onboard
from watcher.net import FetchResult

STOCK_HTML = ('<html><script type="application/ld+json">'
              '{"availability":"http://schema.org/OutOfStock"}</script>'
              "<body><h1>Double X</h1><p>Out of stock</p></body></html>")

GOOD_SPEC = {"mode": "condition", "present": ["In stock"], "absent": ["Out of stock"],
             "min_present": 1, "current_met": False, "notes": "stock check"}


def _script_llm(monkeypatch, replies):
    calls = {"n": 0, "prompts": []}

    def fake_chat_json(cfg, messages, **kw):
        calls["prompts"].append(messages[-1]["content"])
        reply = replies[calls["n"]] if calls["n"] < len(replies) else replies[-1]
        calls["n"] += 1
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return calls


def _fake_fetch(monkeypatch, result):
    monkeypatch.setattr(net, "fetch", lambda url: result)


# ---- build_spec -------------------------------------------------------------

def test_build_spec_happy_path(monkeypatch):
    calls = _script_llm(monkeypatch, [GOOD_SPEC])
    spec, err = onboard.build_spec({"llm": {"max_html_chars": 4000}},
                                   "https://x.example/p", "back in stock", STOCK_HTML)
    assert err is None and spec["mode"] == "condition"
    assert spec["present"] == ["In stock"] and "current_met" not in spec
    assert calls["n"] == 1


def test_build_spec_cross_check_forces_correction(monkeypatch):
    wrong = dict(GOOD_SPEC, current_met=True)         # claims in-stock on an OOS page
    calls = _script_llm(monkeypatch, [wrong, GOOD_SPEC])
    spec, err = onboard.build_spec({"llm": {"max_html_chars": 4000}},
                                   "https://x.example/p", "back in stock", STOCK_HTML)
    assert err is None and calls["n"] == 2
    assert "disagreed" in calls["prompts"][1]          # feedback round happened


def test_build_spec_gives_up_after_two_disagreements(monkeypatch):
    wrong = dict(GOOD_SPEC, current_met=True)
    _script_llm(monkeypatch, [wrong, wrong])
    spec, err = onboard.build_spec({"llm": {"max_html_chars": 4000}},
                                   "https://x.example/p", "back in stock", STOCK_HTML)
    assert spec is None and "cross-check" in err


def test_build_spec_rejects_invalid_then_accepts_fixed(monkeypatch):
    junk = {"mode": "regex", "pattern": ".*", "current_met": False}
    calls = _script_llm(monkeypatch, [junk, GOOD_SPEC])
    spec, err = onboard.build_spec({"llm": {"max_html_chars": 4000}},
                                   "https://x.example/p", "back in stock", STOCK_HTML)
    assert err is None and calls["n"] == 2


def test_build_spec_llm_error_returns_error(monkeypatch):
    _script_llm(monkeypatch, [llm.LLMError("endpoint down")])
    spec, err = onboard.build_spec({"llm": {"max_html_chars": 4000}},
                                   "https://x.example/p", "i", STOCK_HTML)
    assert spec is None and "endpoint down" in err


def test_build_spec_missing_current_met_is_corrected(monkeypatch):
    no_pred = {k: v for k, v in GOOD_SPEC.items() if k != "current_met"}
    calls = _script_llm(monkeypatch, [no_pred, GOOD_SPEC])
    spec, err = onboard.build_spec({"llm": {"max_html_chars": 4000}},
                                   "https://x.example/p", "i", STOCK_HTML)
    assert err is None and calls["n"] == 2


def test_page_digest_includes_jsonld(monkeypatch):
    digest = onboard.page_digest(STOCK_HTML, 3000)
    assert "schema.org/OutOfStock" in digest and "Double X" in digest


# ---- onboard.onboard --------------------------------------------------------

def test_onboard_success(conn, monkeypatch):
    w = dbmod.create_watch(conn, "https://x.example/p", "back in stock", 1440, 1)
    _fake_fetch(monkeypatch, FetchResult(url="u", status="ok", code=200,
                                         text=STOCK_HTML, rung=0))
    _script_llm(monkeypatch, [GOOD_SPEC])
    up = onboard.onboard({"llm": {"max_html_chars": 4000}}, conn,
                         dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "ready"
    assert json.loads(up["spec_json"])["absent"] == ["Out of stock"]
    assert dbmod.list_checks(conn, w["id"])[0]["detail"] == "onboarded; baseline recorded"


def test_onboard_fetch_failure_needs_review(conn, monkeypatch):
    w = dbmod.create_watch(conn, "https://x.example/p", "i", 1440, 1)
    _fake_fetch(monkeypatch, FetchResult(url="u", status="challenged", code=403, rung=1))
    up = onboard.onboard({"llm": {}}, conn, dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "needs_review"
    assert "could not fetch" in up["onboard_error"]
    assert dbmod.list_checks(conn, w["id"])[0]["status"] == "challenged"


def test_onboard_llm_failure_needs_review(conn, monkeypatch):
    w = dbmod.create_watch(conn, "https://x.example/p", "i", 1440, 1)
    _fake_fetch(monkeypatch, FetchResult(url="u", status="ok", code=200,
                                         text=STOCK_HTML, rung=0))
    _script_llm(monkeypatch, [llm.LLMError("unreachable")])
    up = onboard.onboard({"llm": {"max_html_chars": 4000}}, conn,
                         dbmod.get_watch(conn, w["id"]))
    assert up["status"] == "needs_review" and "unreachable" in up["onboard_error"]
