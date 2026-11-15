"""Minimal OpenAI-compatible chat client — stdlib urllib, no SDK.

Works against vLLM/llama.cpp/any /v1 endpoint. Token discipline lives in the
callers: onboarding is the only routine poll path that may call the LLM, and
periodic checks make ZERO calls (test_token_discipline proves it).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

_model_cache: dict[str, str] = {}          # base_url -> resolved model id


class LLMError(Exception):
    pass


def _post(url: str, payload: dict, api_key: str, timeout: float, method: str = "POST") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise LLMError(f"LLM HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"LLM unreachable: {e}") from e
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM returned non-JSON: {e}") from e


def resolve_model(cfg: dict) -> str:
    llm = cfg["llm"]
    if llm.get("model"):
        return llm["model"]
    base = llm["base_url"].rstrip("/")
    if base in _model_cache:
        return _model_cache[base]
    data = _post(base + "/models", {}, llm.get("api_key", ""), 15, method="GET")
    try:
        model = data["data"][0]["id"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"no models at {base}/models") from e
    _model_cache[base] = model
    return model


def chat(cfg: dict, messages: list[dict], *, temperature: float = 0.0,
         max_tokens: int = 1200) -> str:
    llm = cfg["llm"]
    base = llm["base_url"].rstrip("/")
    payload = {
        "model": resolve_model(cfg),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = _post(base + "/chat/completions", payload, llm.get("api_key", ""),
                 llm.get("timeout_seconds", 90))
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"malformed chat response: {str(data)[:300]}") from e


_JSON_RE = re.compile(r"\{.*\}", re.S)


def chat_json(cfg: dict, messages: list[dict], *, temperature: float = 0.0,
              max_tokens: int = 1200) -> dict:
    """Chat + extract the first JSON object. Raises LLMError on garbage."""
    text = chat(cfg, messages, temperature=temperature, max_tokens=max_tokens)
    m = _JSON_RE.search(text or "")
    if not m:
        raise LLMError(f"no JSON object in model reply: {text[:200]!r}")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON in model reply: {e}") from e
    if not isinstance(obj, dict):
        raise LLMError("model reply JSON is not an object")
    return obj
