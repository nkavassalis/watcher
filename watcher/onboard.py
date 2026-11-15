"""Onboarding: turn (URL + plain-English intent) into a deterministic spec.

One LLM call per onboarding (plus one feedback retry). The model sees a
compact page digest — title, JSON-LD blocks, visible text — and must answer
with a spec JSON plus its own prediction of whether the trigger is met on the
CURRENT page. We then evaluate the spec locally; if our verdict disagrees with
the model's prediction the spec gets one correction round, and a second
disagreement lands the watch in needs_review instead of silently watching the
wrong thing.
"""
from __future__ import annotations

import json
import re

from . import evaluate, llm, net

LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)


def page_digest(html: str, max_chars: int) -> str:
    """Title + JSON-LD + visible text, budgeted. JSON-LD matters most for
    commerce pages (schema.org Offer.availability), so it gets its own slice."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    title = " ".join(m.group(1).split()) if m else ""
    ld = "\n".join(b.strip() for b in LDJSON_RE.findall(html or ""))[: max_chars // 3]
    text = evaluate.html_to_text(html)[: max_chars]
    return f"title: {title}\n\njson-ld:\n{ld}\n\nvisible text:\n{text}"


def _prompt(url: str, intent: str, digest: str, feedback: str | None) -> list[dict]:
    system = (
        "You design website change-detection specs. Given a page snapshot and "
        "what the user wants to be told about, reply with exactly ONE JSON "
        "object and nothing else:\n"
        '{"mode":"condition"|"hash",\n'
        ' "present":["string",...],   // texts whose APPEARANCE means the user\'s condition is met\n'
        ' "absent":["string",...],    // texts whose PRESENCE means it is NOT met (e.g. "out of stock")\n'
        ' "min_present":1,\n'
        ' "name":"short human label for this watch, <= 60 chars, e.g. \'Reflx Lab Double X — back in stock\'",\n'
        ' "current_met": true|false, // your judgement: is the condition met on THIS page right now?\n'
        ' "notes":"one short sentence for the user"}\n'
        "Rules: use mode 'hash' only if the user wants any change on the page. "
        "Needles must be short literal strings that appear (or would appear) on "
        "the page — prefer stable machine signals from json-ld (e.g. "
        "schema.org/InStock, schema.org/OutOfStock) over marketing prose. "
        "absent needles must not match when the condition IS met. "
        "The name should identify the thing being watched (product/page title) "
        "plus the intent — not the raw URL."
    )
    user = f"URL: {url}\nUser intent: {intent}\n\nPage snapshot:\n{digest}"
    if feedback:
        user += ("\n\nYour previous spec was wrong: our deterministic check "
                 f"disagreed with your current_met prediction ({feedback}). "
                 "Fix the needles and reply with a corrected JSON object.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_spec(cfg: dict, url: str, intent: str, html: str) -> tuple[dict | None, str | None, str | None]:
    """Returns (spec, suggested_name, error). Raises nothing; LLM failures come
    back as error. The name is a SUGGESTION — the caller decides whether to
    keep it (it only sticks on watches the user never named)."""
    max_chars = cfg["llm"].get("max_html_chars", 24000)
    digest = page_digest(html, max_chars)
    feedback = None
    for _ in range(2):                       # initial attempt + one correction round
        try:
            raw = llm.chat_json(cfg, _prompt(url, intent, digest, feedback))
        except llm.LLMError as e:
            return None, None, str(e)
        current_met = raw.pop("current_met", None)
        suggested = raw.pop("name", None)
        if not isinstance(suggested, str) or not suggested.strip():
            suggested = None
        else:
            suggested = " ".join(suggested.split())[:60]
        err = evaluate.validate_spec(raw)
        if err:
            feedback = f"invalid spec: {err}"
            continue
        spec = evaluate.normalize_spec(raw)
        verdict = evaluate.evaluate(spec, html, None)["status"] == "changed"
        if not isinstance(current_met, bool):
            feedback = "missing or non-boolean current_met"
            continue
        if verdict == current_met:
            return spec, suggested, None
        feedback = (f"local evaluation says condition is "
                    f"{'MET' if verdict else 'NOT met'} on this page, "
                    f"you predicted current_met={current_met}")
    return None, None, f"spec failed cross-check after 2 attempts: {feedback}"


def onboard(cfg: dict, conn, watch: dict) -> dict:
    """Fetch + build spec + persist. Never raises; outcome is on the watch row."""
    result = net.fetch(watch["url"])
    if not result.ok:
        reason = result.error or f"fetch {result.status} (HTTP {result.code})"
        from . import db as dbmod
        dbmod.record_check(conn, watch["id"], result.status, rung=result.rung,
                           detail=f"onboard fetch failed: {reason}")
        return dbmod.set_spec(conn, watch["id"], None, "needs_review",
                              f"could not fetch page: {reason}")
    spec, suggested_name, err = build_spec(cfg, watch["url"], watch["intent"], result.text)
    from . import db as dbmod
    if err:
        return dbmod.set_spec(conn, watch["id"], None, "needs_review", err)
    # The LLM name only sticks while the watch is unnamed, or already carries
    # an auto-name (a re-fit may improve it). A user-typed name is never touched.
    if suggested_name and (watch["name"] is None or watch["name"] == watch["url"]
                           or watch.get("name_is_auto")):
        dbmod.update_watch(conn, watch["id"],
                           {"name": suggested_name, "name_is_auto": 1})
    dbmod.record_check(conn, watch["id"], "ok", rung=result.rung,
                       detail="detector fitted; watching")
    return dbmod.set_spec(conn, watch["id"], json.dumps(spec), "ready", None)
