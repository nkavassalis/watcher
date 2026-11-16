"""Email notifications via the Gmail API — same shape as sparky's send_mail.py:
stored OAuth tokens (client_secret.json + tokens.json in gmail_dir), refresh the
access token, base64url a MIMEText, POST to gmail/v1/users/me/messages/send.
No google SDK, no external deps.

gmail_dir comes from config (~/.gmail by default) and is expanded at runtime;
nothing here is hardcoded to a machine. If the token files are missing or the
refresh fails, send() raises NotifyError and the caller degrades to in-app
status only — a missing mailbox must never kill a poll.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate


class NotifyError(Exception):
    pass


def _get_access_token(gmail_dir: str) -> str:
    creds_path = os.path.join(gmail_dir, "client_secret.json")
    tok_path = os.path.join(gmail_dir, "tokens.json")
    try:
        with open(creds_path, encoding="utf-8") as fh:
            creds = json.load(fh)["installed"]
        with open(tok_path, encoding="utf-8") as fh:
            tok = json.load(fh)
    except (OSError, KeyError, json.JSONDecodeError) as e:
        raise NotifyError(f"gmail credentials unavailable: {e}") from e
    data = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }
    req = urllib.request.Request(
        creds["token_uri"], data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:
        raise NotifyError(f"gmail token refresh failed: {e}") from e
    tok["access_token"] = resp["access_token"]
    tok["expires_at"] = time.time() + resp["expires_in"] - 60
    try:
        with open(tok_path, "w", encoding="utf-8") as fh:
            json.dump(tok, fh, indent=2)   # cache like sparky; non-fatal
    except OSError:
        pass
    return resp["access_token"]


def send(cfg: dict, subject: str, body: str, *, html: bool = False) -> str:
    """Send one notification email. Returns the Gmail message id."""
    ecfg = cfg["email"]
    if not ecfg.get("enabled"):
        raise NotifyError("email notifications disabled")
    if not ecfg.get("to"):
        raise NotifyError("no recipient configured (email.to)")
    gmail_dir = os.path.expanduser(ecfg.get("gmail_dir", "~/.gmail"))

    msg = MIMEText(body, "html" if html else "plain")
    from_addr = ecfg.get("from_addr") or "me"
    msg["From"] = formataddr((ecfg.get("from_name") or "Watcher", from_addr))
    msg["To"] = ecfg["to"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    access = _get_access_token(gmail_dir)
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps({"raw": raw}).encode(),
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        method="POST")
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:
        raise NotifyError(f"gmail send failed: {e}") from e
    return resp.get("id", "?")


def notify_change(cfg: dict, watch: dict, detail: str) -> str | None:
    """Best-effort email on a triggered watch. Returns message id or None
    (disabled / not configured); raises NotifyError only on real send failures."""
    ecfg = cfg["email"]
    if not ecfg.get("enabled") or not ecfg.get("to"):
        return None
    subject = f"Watcher: {watch['url']} — {watch['intent']}"
    body = (f"Change detected on:\n\n  {watch['url']}\n\n"
            f"What you asked to watch for: {watch['intent']}\n"
            f"Check result: {detail}\n")
    return send(cfg, subject, body)


def notify_refit(cfg: dict, watch: dict, ok: bool, reason: str) -> str | None:
    """Operational alert for the auto re-fit lifecycle (Nick: ALWAYS email
    when a re-fit runs, and when it fails). Sent regardless of the per-watch
    notify_email toggle — a blind detector is not a 'change' the user opted
    out of. Respects only the global email.enabled/to switches."""
    ecfg = cfg["email"]
    if not ecfg.get("enabled") or not ecfg.get("to"):
        return None
    outcome = "succeeded" if ok else "FAILED"
    subject = f"Watcher: {watch['url']} — detector auto-refit {outcome}"
    body = (f"The detector for this watch went blind (none of its needles survived "
            f"a page change) and Watcher automatically re-fitted it with the LLM.\n\n"
            f"URL: {watch['url']}\n"
            f"What you asked to watch for: {watch['intent']}\n"
            f"Outcome: {outcome}\n"
            f"Details: {reason}\n")
    return send(cfg, subject, body)
