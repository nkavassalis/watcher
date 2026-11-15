"""Gmail notify: MIME construction + token refresh flow, fully faked.
Nothing here may touch the network or the real ~/.gmail.
"""
import base64
import json
from pathlib import Path

import pytest

from watcher import notify


@pytest.fixture()
def gmail_home(tmp_path):
    d = tmp_path / "gmail"
    d.mkdir()
    (d / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "cid", "client_secret": "csec",
        "token_uri": "https://oauth.example/token"}}))
    (d / "tokens.json").write_text(json.dumps({"refresh_token": "rtok"}))
    return str(d)


@pytest.fixture()
def cfg(gmail_home):
    return {"email": {"enabled": True, "to": "nick@example.com",
                      "from_addr": "watcher@example.com", "from_name": "Watcher",
                      "gmail_dir": gmail_home}}


class Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fake_urlopen(monkeypatch):
    reqs = []

    def fake(req, timeout=None):
        reqs.append(req)
        if "token" in req.full_url:
            return Resp({"access_token": "atok", "expires_in": 3600})
        return Resp({"id": "msg-42", "labelIds": ["SENT"]})

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    return reqs


def test_send_builds_mime_and_posts(cfg, fake_urlopen, gmail_home):
    mid = notify.send(cfg, "Restock!", "It is back")
    assert mid == "msg-42"
    assert fake_urlopen[0].full_url == "https://oauth.example/token"
    sent = fake_urlopen[1]
    assert sent.full_url.endswith("/gmail/v1/users/me/messages/send")
    assert sent.get_header("Authorization") == "Bearer atok"
    raw = json.loads(sent.data.decode())["raw"]
    msg = base64.urlsafe_b64decode(raw).decode()
    assert "To: nick@example.com" in msg
    assert "Subject: Restock!" in msg
    assert "It is back" in msg
    assert "Watcher <watcher@example.com>" in msg
    # token cached back to disk like sparky does
    tok = json.loads((Path(gmail_home) / "tokens.json").read_text())
    assert tok["access_token"] == "atok"


def test_send_disabled_raises(cfg):
    cfg["email"]["enabled"] = False
    with pytest.raises(notify.NotifyError, match="disabled"):
        notify.send(cfg, "s", "b")


def test_send_without_recipient_raises(cfg):
    cfg["email"]["to"] = ""
    with pytest.raises(notify.NotifyError, match="recipient"):
        notify.send(cfg, "s", "b")


def test_missing_credentials_raise(cfg, tmp_path):
    cfg["email"]["gmail_dir"] = str(tmp_path / "nowhere")
    with pytest.raises(notify.NotifyError, match="credentials"):
        notify.send(cfg, "s", "b")


def test_token_refresh_failure_raises(cfg, monkeypatch, gmail_home):
    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    with pytest.raises(notify.NotifyError, match="refresh failed"):
        notify.send(cfg, "s", "b")


def test_notify_change_skips_when_disabled(cfg):
    cfg["email"]["enabled"] = False
    assert notify.notify_change(cfg, {"url": "u", "intent": "i"}, "d") is None


def test_notify_change_formats_message(cfg, fake_urlopen):
    mid = notify.notify_change(cfg, {"url": "https://x.example/p",
                                     "intent": "back in stock"}, "trigger condition met")
    assert mid == "msg-42"
