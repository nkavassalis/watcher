"""Detection specs — deterministic, LLM-free evaluation.

A spec is JSON produced once at onboarding and evaluated on every poll with
ZERO LLM calls (token discipline, house invariant 11). Two modes:

  condition: trigger when (no `absent` needle is present) AND (at least
             `min_present` of the `present` needles appear) in the haystack.
             haystack = raw HTML + normalized visible text, matched
             case-insensitively. Raw HTML is included so JSON-LD signals
             (schema.org availability) count too.
  hash:      trigger when the normalized-text fingerprint changes — the
             generic "something changed on this page" watch.

Needles come from the model but are validated structurally before use; the
model never supplies code, regexes, or selectors.
"""
from __future__ import annotations

import hashlib

from .textutil import html_to_text, normalize

MAX_NEEDLES = 12
MAX_NEEDLE_LEN = 120


def haystack(html: str) -> str:
    return ((html or "") + "\n" + html_to_text(html or "")).lower()


def fingerprint(html: str) -> str:
    return hashlib.sha256(normalize(html_to_text(html)).encode()).hexdigest()


def validate_spec(spec) -> str | None:
    """None if valid, else a human-readable reason it is not."""
    if not isinstance(spec, dict):
        return "spec is not an object"
    mode = spec.get("mode")
    if mode not in ("condition", "hash"):
        return "mode must be 'condition' or 'hash'"
    if mode == "hash":
        return None
    for key in ("present", "absent"):
        v = spec.get(key, [])
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            return f"{key} must be a list of strings"
        if len(v) > MAX_NEEDLES:
            return f"{key} has too many needles (max {MAX_NEEDLES})"
        if any(not x.strip() or len(x) > MAX_NEEDLE_LEN for x in v):
            return f"{key} needles must be non-empty and <= {MAX_NEEDLE_LEN} chars"
    if not spec.get("present") and not spec.get("absent"):
        return "condition spec needs at least one needle in present or absent"
    mp = spec.get("min_present", 1)
    if not isinstance(mp, int) or isinstance(mp, bool) or mp < 1:
        return "min_present must be an integer >= 1"
    if mp > len(spec.get("present", [])):
        return "min_present exceeds the number of present needles"
    return None


def normalize_spec(spec: dict) -> dict:
    return {
        "mode": spec["mode"],
        "present": [s.strip() for s in spec.get("present", []) if s.strip()],
        "absent": [s.strip() for s in spec.get("absent", []) if s.strip()],
        "min_present": max(1, int(spec.get("min_present", 1))),
        "notes": str(spec.get("notes", ""))[:500],
    }


def evaluate(spec: dict, html: str, baseline_hash: str | None) -> dict:
    """Returns {status, detail, matched, snapshot_hash, baseline_hash}.
    status: 'ok' (no trigger) | 'changed' (trigger fired)."""
    hay = haystack(html)
    if spec["mode"] == "hash":
        h = fingerprint(html)
        changed = baseline_hash is not None and h != baseline_hash
        return {
            "status": "changed" if changed else "ok",
            "detail": "yes — page content changed" if changed else "no change since last check",
            "matched": None,
            "snapshot_hash": h,
            "baseline_hash": h,
        }

    present_hits = [n for n in spec.get("present", []) if n.lower() in hay]
    absent_hits = [n for n in spec.get("absent", []) if n.lower() in hay]
    min_present = max(1, int(spec.get("min_present", 1)))
    triggered = (not absent_hits) and (
        len(present_hits) >= min_present if spec.get("present") else True)
    matched = {"present_hits": present_hits, "absent_hits": absent_hits}
    # User-facing verdicts: what happened, in words a human reads once and
    # never has to decode. The raw needles stay in matched_json for debugging.
    if triggered:
        detail = ("yes — found: " + ", ".join(present_hits[:3])
                  if present_hits else "yes — condition met")
    elif absent_hits:
        detail = "not yet — still shows: " + ", ".join(absent_hits[:3])
    else:
        detail = "not yet — waiting for: " + ", ".join(spec.get("present", [])[:4])
    return {
        "status": "changed" if triggered else "ok",
        "detail": detail,
        "matched": matched,
        "snapshot_hash": fingerprint(html),
        "baseline_hash": None,
    }
