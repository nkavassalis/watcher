"""Ladder rungs 0-1 with a fake requests.get: cookie jar, challenge retry
semantics (ONE attempt for streak logic), block vs challenge, error paths.
"""
import pytest

from watcher import net


class FakeResp:
    def __init__(self, status_code=200, text="<html>ok</html>", headers=None,
                 cookies=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        if cookies is not None:
            self.cookies = FakeCookies(cookies)
        # no .cookies attribute at all is legal — getattr-guarded in net


class FakeCookies(dict):
    def items(self):
        return dict.items(self)


CALLS: list = []


@pytest.fixture()
def patch_get(monkeypatch):
    CALLS.clear()
    holder = {"queue": []}

    def fake_get(url, **kw):
        CALLS.append((url, kw))
        resp = holder["queue"].pop(0) if holder["queue"] else FakeResp()
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(net.requests, "get", fake_get)
    net.reset_for_tests()
    cfg = {"fetch": {"challenge_wait_seconds": 0, "browser_enabled": False,
                     "timeout_seconds": 5}}
    net.configure(cfg)
    return holder


CHALLENGE = FakeResp(403, "<html>Just a moment... Cloudflare</html>",
                     headers={"cf-mitigated": "challenge"})


def test_plain_ok_is_one_call_rung0(patch_get):
    res = net.fetch("https://a.example/p")
    assert res.status == "ok" and res.rung == 0 and res.code == 200
    assert len(CALLS) == 1


def test_cookie_jar_roundtrip(patch_get):
    patch_get["queue"] = [
        FakeResp(200, "first", cookies={"__cf_bm": "abc"}),
        FakeResp(200, "second"),
    ]
    net.fetch("https://cf.example/p")
    net.fetch("https://cf.example/p")
    assert CALLS[1][1]["cookies"] == {"__cf_bm": "abc"}      # echoed on next visit
    assert net._jar["cf.example"]["__cf_bm"] == "abc"


def test_cookie_jar_is_per_host(patch_get):
    patch_get["queue"] = [FakeResp(200, "x", cookies={"k": "v"}), FakeResp()]
    net.fetch("https://one.example/p")
    net.fetch("https://two.example/p")
    assert "cookies" not in CALLS[1][1]


def test_response_without_cookies_attr_survives(patch_get):
    class NoCookies:
        status_code, headers = 200, {}
        text = "ok"
    patch_get["queue"] = [NoCookies()]
    assert net.fetch("https://a.example/p").status == "ok"


def test_challenge_retries_once_then_degrades_to_browser(patch_get):
    patch_get["queue"] = [CHALLENGE, CHALLENGE]
    res = net.fetch("https://cf.example/p")
    assert res.status == "challenged" and res.rung == 1
    assert len(CALLS) == 2                       # ONE attempt: initial + one retry
    # second call carried the fuller browser header set
    assert "upgrade-insecure-requests" in CALLS[1][1]["headers"]


def test_challenge_then_cleared(patch_get):
    patch_get["queue"] = [CHALLENGE, FakeResp(200, "<html>real</html>")]
    res = net.fetch("https://cf.example/p")
    assert res.status == "ok" and res.rung == 1 and "real" in res.text


def test_block_is_not_retried_and_backs_off_the_host(patch_get):
    patch_get["queue"] = [FakeResp(403, "forbidden")]
    res = net.fetch("https://mean.example/p")
    assert res.status == "blocked" and res.code == 403
    assert len(CALLS) == 1                       # a block is not a challenge


def test_http_500_is_error(patch_get):
    patch_get["queue"] = [FakeResp(500, "boom")]
    res = net.fetch("https://a.example/p")
    assert res.status == "error" and res.code == 500


def test_connection_error_is_error(patch_get):
    patch_get["queue"] = [net.requests.ConnectionError("down")]
    res = net.fetch("https://a.example/p")
    assert res.status == "error" and "down" in res.error


def test_challenge_detection_variants():
    assert net.is_challenge(503, {}, "<p>Just a moment... Cloudflare</p>")
    assert net.is_challenge(403, {"cf-mitigated": "challenge"}, "whatever")
    assert net.is_challenge(429, {}, "challenge-platform script")
    assert not net.is_challenge(200, {}, "just a moment cloudflare")
    assert not net.is_challenge(403, {}, "plain forbidden")
    assert net.is_block(429, {}, "rate limited")
    assert not net.is_block(403, {"cf-mitigated": "challenge"}, "just a moment cloudflare")


def test_browser_rung_skipped_when_disabled_or_missing(patch_get, monkeypatch):
    patch_get["queue"] = [CHALLENGE, CHALLENGE]
    assert net.fetch("https://cf.example/p").status == "challenged"


def test_looks_unreal():
    from watcher.net import looks_unreal
    assert looks_unreal(200, "<html>too short to be a page</html>")
    assert looks_unreal(200, "<html><h1>Just a Moment</h1>" + "x" * 400 + "</html>")
    assert looks_unreal(200, "<html>" + " " * 400 + "</html>")   # whitespace-only
    assert not looks_unreal(200, "<html><body>" + "real product content " * 40
                            + "</body></html>")
    assert not looks_unreal(404, "x")        # non-200 is not this function's job
