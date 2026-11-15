"""Bot-check ladder rungs 2-4 (see the bot-check-ladder skill).

Rung 2  persistent-context headless Chromium per host (Cloudflare accumulates
        trust across visits); real Chrome channel first, bundled Chromium after.
Rung 3  rules-based consent/modal dismissal — no model, max 3 clicks.
Rung 4  LLM rescue with a HARD action allowlist. The model only ever chooses
        among actions the code permits: click (text must verifiably exist in
        the snapshot we collected), wait, give_up. There is no type/submit/
        navigate action to ask for. Unknown action / non-JSON / LLM error all
        resolve to give_up, never to more autonomy. The fetched page is
        untrusted input; the gate is what the code permits, not the model's
        reasoning trace.

playwright is imported at module top so net.py can treat it as an optional
runtime dependency (ImportError -> ladder stops at rung 1).
"""
from __future__ import annotations

import os
import re
import time

from playwright.sync_api import sync_playwright

from . import llm
from .net import FetchResult, _cookies_for, host_of

MODAL_PATTERNS = [
    r"accept( all )?cookies", r"^i agree$", r"got it", r"continue as guest",
    r"^no thanks$", r"^close$", r"dismiss",
]
MAX_MODAL_CLICKS = 3

STUCK_RE = re.compile(
    r"just a moment|verify you are human|are you a robot|enable javascript"
    r"|access denied|pardon our interruption", re.I)
MIN_CONTENT_CHARS = 1500


def _profile_dir(cfg: dict, host: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]", "_", host)
    return os.path.join(cfg["fetch"].get("browser_profile_dir", "data/browser-profiles"), safe)


def _looks_stuck(title: str, body: str, content: str) -> bool:
    return bool(STUCK_RE.search((title or "") + " " + (body or "")[:2000])) or \
        len(content or "") < MIN_CONTENT_CHARS


# ---- rung 3: rules, no model ---------------------------------------------------

def dismiss_modals(page) -> int:
    """Click known consent/modal buttons deterministically. Max 3 clicks."""
    clicks = 0
    for pat in MODAL_PATTERNS:
        if clicks >= MAX_MODAL_CLICKS:
            break
        rx = re.compile(pat, re.I)
        for role in ("button", "link"):
            if clicks >= MAX_MODAL_CLICKS:
                break
            try:
                el = page.get_by_role(role, name=rx).first
                if el.is_visible():
                    el.click(timeout=3000)
                    clicks += 1
                    time.sleep(0.4)
                    break
            except Exception:
                continue
    return clicks


# ---- rung 4: gated LLM rescue ------------------------------------------------------

def snapshot(page, excerpt_chars: int = 1200) -> dict:
    """Compact, code-collected view of the page. The rescue may only reference
    control texts that appear in this snapshot."""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    controls: list[str] = []
    try:
        for t in page.locator("button, a[role=button], input[type=submit]").all_inner_texts():
            t = t.strip()
            if t and t not in controls:
                controls.append(t)
    except Exception:
        pass
    try:
        excerpt = (page.inner_text("body") or "")[:excerpt_chars]
    except Exception:
        excerpt = ""
    return {"title": title, "controls": controls[:40], "excerpt": excerpt}


def _click_text(page, text: str) -> bool:
    for role in ("button", "link"):
        try:
            el = page.get_by_role(role, name=text).first
            if el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _rescue_prompt(snap: dict, intent: str | None) -> list[dict]:
    ask = (f"The user wants to watch this page for: {intent}. " if intent else "") + \
        "Get past the interstitial so the real page content is visible."
    system = (
        "You are stuck on a web interstitial (cookie consent / bot check / modal). "
        "Reply with exactly ONE JSON action per step, nothing else:\n"
        '{"action":"click","text":"<exact control text from the snapshot>"}\n'
        '{"action":"wait","seconds":3}\n'
        '{"action":"give_up"}\n'
        "Only click texts that appear verbatim in the snapshot controls list. "
        "There is no type, submit, or navigate action. If unsure, give_up."
    )
    user = (f"Goal: {ask}\n\nSnapshot:\n"
            f"title: {snap['title']}\n"
            f"controls: {snap['controls']}\n"
            f"excerpt: {snap['excerpt']}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def rescue(page, cfg: dict, intent: str | None = None) -> str:
    """Returns 'ok' (rescue thinks page is readable) or 'gave_up'. Hard caps:
    steps and wall-clock; every anomaly resolves to give_up."""
    fcfg = cfg["fetch"]
    max_steps = fcfg.get("rescue_max_steps", 4)
    wall = fcfg.get("rescue_wall_seconds", 45)
    start = time.monotonic()
    snap = snapshot(page)
    for _ in range(max_steps):
        if time.monotonic() - start > wall:
            return "gave_up"
        try:
            action = llm.chat_json(cfg, _rescue_prompt(snap, intent), max_tokens=200)
        except llm.LLMError:
            return "gave_up"
        kind = action.get("action")
        if kind == "give_up":
            return "gave_up"
        if kind == "wait":
            secs = action.get("seconds", 3)
            time.sleep(min(max(float(secs), 0), 5) if isinstance(secs, (int, float)) else 1)
        elif kind == "click":
            text = action.get("text")
            # THE GATE: the text must verifiably exist in the snapshot we
            # collected. A hallucinated text is an instant give_up, never a click.
            if not isinstance(text, str) or text not in snap["controls"]:
                return "gave_up"
            if not _click_text(page, text):
                return "gave_up"
            time.sleep(0.8)
        else:
            return "gave_up"                       # unknown action -> no more autonomy
        snap = snapshot(page)
        try:
            content = page.content() or ""
        except Exception:
            content = snap["excerpt"]
        if not _looks_stuck(snap["title"], snap["excerpt"], content):
            return "ok"
    return "gave_up"


# ---- rung 2: persistent-context browser ---------------------------------------------

def fetch_browser(url: str, cfg: dict, jar: dict, intent: str | None = None) -> FetchResult:
    """Open the URL in a persistent per-host context; climb rungs 3-4 if needed.
    Never raises."""
    fcfg = cfg["fetch"]
    host = host_of(url)
    timeout_ms = int(fcfg.get("browser_timeout_seconds", 45) * 1000)
    ua = fcfg.get("user_agent")
    last_err = "browser unavailable"
    try:
        with sync_playwright() as p:
            for channel in ("chrome", None):
                try:
                    ctx = p.chromium.launch_persistent_context(
                        _profile_dir(cfg, host),
                        headless=True, channel=channel,
                        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                        locale="en-CA", user_agent=ua)
                except Exception as e:
                    last_err = f"launch failed ({channel or 'chromium'}): {e}"
                    continue
                try:
                    ctx.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                    # seed the rung-0 jar in
                    seeded = _cookies_for(host)
                    if seeded:
                        ctx.add_cookies([{"name": k, "value": v, "url": url}
                                         for k, v in seeded.items()])
                    page = ctx.new_page()
                    try:
                        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                        page.wait_for_timeout(2500)   # let JS challenges settle
                    except Exception as e:
                        last_err = f"goto failed: {e}"
                        ctx.close()
                        continue
                    rung = 2
                    dismiss_modals(page)              # rung 3
                    content = page.content()
                    title = page.title() or ""
                    body = page.inner_text("body") if not _looks_stuck(title, "", content) else ""
                    if _looks_stuck(title, body, content):
                        if rescue(page, cfg, intent) == "ok":   # rung 4
                            rung = 4
                            content = page.content()
                        else:
                            ctx.close()
                            return FetchResult(url=url, status="challenged", rung=2,
                                               error="still challenged after rescue")
                    # harvest cookies back into the jar
                    try:
                        for c in ctx.cookies():
                            jar.setdefault(host, {})[c["name"]] = c["value"]
                    except Exception:
                        pass
                    ctx.close()
                    return FetchResult(url=url, status="ok", code=200, text=content,
                                       rung=rung)
                except Exception as e:
                    last_err = f"browser error: {e}"
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    continue
    except Exception as e:
        last_err = f"playwright unavailable: {e}"
    return FetchResult(url=url, status="error", error=last_err)
