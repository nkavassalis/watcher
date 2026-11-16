"""The one outbound path + the bot-check ladder (see the bot-check-ladder skill).

Rung 0  per-host cookie jar — the fix that actually matters for Cloudflare.
Rung 1  one delayed retry with fuller browser headers on a challenge.
Rung 2  persistent-context headless Chromium (browser.py), per-host cooldown.
Rung 3  rules-based modal dismissal (browser.py, no model).
Rung 4  LLM rescue, hard action allowlist (browser.py, gated).

`requests.get` stays the seam: tests monkeypatch it and fake responses need
only .status_code/.headers/.text and optionally .cookies. Retry/fallback
knobs are OFF until configure() runs, so the hermetic suite keeps
single-request semantics.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

# challenge vs ban: a challenge is a machine problem (wait + browser headers);
# 403/429 without a challenge signature is a block -> host backoff.
BLOCK_STATUSES = (403, 429)

_jar: dict[str, dict[str, str]] = {}          # host -> {name: value}
_last_browser_launch: dict[str, float] = {}   # host -> monotonic of last rung-2 launch

# knobs live here but stay inert until configure(cfg) — see module docstring
_cfg: dict = {}


def configure(cfg: dict) -> None:
    """cfg is the whole app config; fetch/browser read from it."""
    global _cfg
    _cfg = cfg


def reset_for_tests() -> None:
    _jar.clear()
    _last_browser_launch.clear()
    _cfg = {}


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "?").lower()


@dataclass
class FetchResult:
    url: str
    status: str = "error"          # ok|challenged|blocked|error
    code: int | None = None
    text: str = ""                 # raw HTML when ok
    rung: int | None = None
    error: str | None = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---- rung 0: cookie jar -------------------------------------------------------

def _cookies_for(host: str) -> dict:
    return dict(_jar.get(host, {}))


def _store_cookies(host: str, resp) -> None:
    cookies = getattr(resp, "cookies", None)
    if not cookies:
        return
    try:
        items = cookies.items() if hasattr(cookies, "items") else resp.cookies.get_dict().items()
    except Exception:
        return
    if items:
        _jar.setdefault(host, {}).update(dict(items))


def _headers(extra: dict | None = None) -> dict:
    h = {
        "user-agent": _cfg.get("fetch", {}).get(
            "user_agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-CA,en;q=0.9",
    }
    h.update(extra or {})
    return h


def _http_get(url: str, headers: dict, timeout: float):
    """One requests.get with the host's cookies attached (the seam for tests)."""
    host = host_of(url)
    kw: dict = {"headers": headers, "timeout": timeout, "allow_redirects": True}
    ck = _cookies_for(host)
    if ck:
        kw["cookies"] = ck
    resp = requests.get(url, **kw)
    _store_cookies(host, resp)
    return resp


# ---- challenge / block detection ------------------------------------------------

def is_challenge(code: int, headers, body: str) -> bool:
    headers = headers or {}
    try:
        get = headers.get
    except AttributeError:
        return False
    if code in (403, 503) and str(get("cf-mitigated", "")).lower() == "challenge":
        return True
    if code not in (403, 503, 429):
        return False
    head = (body or "")[:4096].lower()
    return ("just a moment" in head and "cloudflare" in head) or "challenge-platform" in head


def is_block(code: int, headers, body: str) -> bool:
    return code in BLOCK_STATUSES and not is_challenge(code, headers, body)


# HTTP 200s that are really an outage or a soft bot wall: error pages and
# interstitials served with a success code. Used ONLY to suppress staleness
# verdicts and to refuse re-fitting — never on the trigger path — so the
# signature list is deliberately biased toward MISSING staleness, never
# firing a re-fit because a real page happened to mention "rate limit".
OUTAGE_SIGNATURES = (
    "just a moment", "attention required", "enable javascript and cookies",
    "service unavailable", "bad gateway", "gateway timeout", "too many requests",
    "rate limit", "access denied", "are you a robot", "temporarily unavailable",
    "server error", "checking your browser", "verify you are a human",
)


def looks_unreal(code: int, body: str) -> bool:
    """A 200 that is not really the page: too short to be a page, or carrying
    an outage/bot-wall signature in its head."""
    if code != 200:
        return False
    text = body or ""
    if len("".join(text.split())) < 300:   # all-whitespace padding counts as empty
        return True
    head = text[:4096].lower()
    return any(s in head for s in OUTAGE_SIGNATURES)


FULL_BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "accept-language": "en-CA,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",
    "upgrade-insecure-requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}


# ---- rungs 0-1: stateless HTTP ---------------------------------------------------

def fetch_http(url: str) -> tuple[FetchResult, bool]:
    """Returns (result, needs_browser). needs_browser=True means still challenged."""
    fcfg = _cfg.get("fetch", {})
    timeout = fcfg.get("timeout_seconds", 25)
    rung = 0
    try:
        resp = _http_get(url, _headers(), timeout)
    except requests.RequestException as e:
        return FetchResult(url=url, status="error", error=f"fetch failed: {e}"), False

    if is_challenge(resp.status_code, resp.headers, resp.text):
        # rung 1: wait once, retry once with fuller headers. A still-challenged
        # response is returned as-is so the caller counts ONE attempt.
        wait = fcfg.get("challenge_wait_seconds", 20)
        if wait:
            time.sleep(wait)
        try:
            resp = _http_get(url, _headers(FULL_BROWSER_HEADERS), timeout)
        except requests.RequestException as e:
            return FetchResult(url=url, status="error", error=f"fetch failed: {e}"), True
        if is_challenge(resp.status_code, resp.headers, resp.text):
            return (FetchResult(url=url, status="challenged", code=resp.status_code,
                                text=resp.text, rung=1), True)
        if is_block(resp.status_code, resp.headers, resp.text):
            return FetchResult(url=url, status="blocked", code=resp.status_code,
                               text=resp.text, rung=1), False
        rung = 1

    if is_block(resp.status_code, resp.headers, resp.text):
        return FetchResult(url=url, status="blocked", code=resp.status_code,
                           text=resp.text, rung=rung), False
    if resp.status_code >= 400:
        return FetchResult(url=url, status="error", code=resp.status_code,
                           error=f"HTTP {resp.status_code}", rung=rung), False

    return FetchResult(url=url, status="ok", code=resp.status_code, text=resp.text,
                       rung=rung, headers=dict(resp.headers or {})), False


# ---- the ladder --------------------------------------------------------------------

def fetch(url: str) -> FetchResult:
    """Climb the ladder for one URL. Never raises; callers get a FetchResult."""
    result, needs_browser = fetch_http(url)
    if not needs_browser:
        return result

    fcfg = _cfg.get("fetch", {})
    if not fcfg.get("browser_enabled", True):
        return result
    try:
        from . import browser  # playwright is an optional runtime dep
    except ImportError:
        return result

    host = host_of(url)
    cooldown = fcfg.get("browser_launch_cooldown_seconds", 600)
    now = time.monotonic()
    if now - _last_browser_launch.get(host, -1e9) < cooldown:
        return result                      # rung-2 cooldown: a stuck site can't fork a browser per second
    _last_browser_launch[host] = now
    return browser.fetch_browser(url, _cfg, _jar)
