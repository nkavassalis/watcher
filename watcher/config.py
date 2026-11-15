"""Config: one JSON file deep-merged over DEFAULTS, .env for overrides.

Secrets (LLM api key, gmail dir) never reach the admin UI unmasked —
masked_config() is the only thing web.py shows. The shipped DEFAULTS carry
loopback placeholders only; the real LLM endpoint lives in the gitignored
config.json / .env so the repo never contains internal hosts.
"""
from __future__ import annotations

import copy
import json
import os

DEFAULTS: dict = {
    "llm": {
        "base_url": "http://127.0.0.1:8000/v1",   # any OpenAI-compatible endpoint
        "model": "",                                # "" = whatever /models reports first
        "api_key": "",
        "timeout_seconds": 90,
        "max_html_chars": 24000,                    # page text budget sent to the model
    },
    "email": {
        "enabled": False,
        "to": "",
        "from_addr": "",                            # must be configured in Gmail "Send as"
        "from_name": "Watcher",
        "gmail_dir": "~/.gmail",                    # client_secret.json + tokens.json live here
    },
    "fetch": {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "timeout_seconds": 25,
        "challenge_wait_seconds": 20,               # rung 1: one delayed retry
        "browser_enabled": True,                    # rungs 2-4 (needs playwright)
        "browser_profile_dir": "data/browser-profiles",
        "browser_launch_cooldown_seconds": 600,     # per host, so a stuck site can't fork a browser per second
        "browser_timeout_seconds": 45,
        "rescue_max_steps": 4,
        "rescue_wall_seconds": 45,
        "block_backoff_base_seconds": 3600,         # 403/429: pause the source, doubling
        "block_backoff_max_seconds": 86400,
    },
    "scheduler": {
        "tick_seconds": 30,
        "default_interval_minutes": 1440,           # once a day unless the watch says otherwise
        "min_interval_minutes": 5,
    },
}

SECRET_KEYS = {("llm", "api_key")}                  # (section, key) shown masked in the UI


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> dict:
    """DEFAULTS <- config.json <- environment."""
    cfg = copy.deepcopy(DEFAULTS)
    path = path or os.environ.get("WATCHER_CONFIG", "config.json")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = _deep_merge(cfg, json.load(fh) or {})

    env = os.environ
    if env.get("WATCHER_LLM_BASE_URL"):
        cfg["llm"]["base_url"] = env["WATCHER_LLM_BASE_URL"]
    if env.get("WATCHER_LLM_MODEL"):
        cfg["llm"]["model"] = env["WATCHER_LLM_MODEL"]
    if env.get("WATCHER_LLM_API_KEY"):
        cfg["llm"]["api_key"] = env["WATCHER_LLM_API_KEY"]
    if env.get("WATCHER_GMAIL_DIR"):
        cfg["email"]["gmail_dir"] = env["WATCHER_GMAIL_DIR"]
    return cfg


def save(path: str, cfg: dict) -> None:
    """Persist the editable subset of config (used by the settings UI)."""
    editable = {
        "llm": {k: cfg["llm"].get(k) for k in ("base_url", "model", "api_key", "timeout_seconds", "max_html_chars")},
        "email": {k: cfg["email"].get(k) for k in ("enabled", "to", "from_addr", "from_name", "gmail_dir")},
        "scheduler": {k: cfg["scheduler"].get(k) for k in ("default_interval_minutes", "min_interval_minutes")},
    }
    # never launder an env-provided secret onto disk just because someone
    # clicked Save in the settings UI
    env_key = os.environ.get("WATCHER_LLM_API_KEY")
    if env_key and editable["llm"].get("api_key") == env_key:
        editable["llm"]["api_key"] = ""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(editable, fh, indent=2)


def masked_config(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for section, key in SECRET_KEYS:
        if out.get(section, {}).get(key):
            out[section][key] = "***"
    return out
