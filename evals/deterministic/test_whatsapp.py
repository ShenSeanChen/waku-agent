"""DETERMINISTIC EVAL — the WhatsApp webhook's security boundary.

The gateway verifies X-Hub-Signature-256 before touching a webhook payload —
that HMAC check is the only thing standing between the public internet and
waku.respond(). PR #26 added it and explicitly asked for a regression test
proving a forged POST gets a 403 and never reaches the agent. This file pins
the whole boundary offline:

  * a POST with a missing or forged signature → 403, agent never invoked
  * a correctly signed text message → 200, agent invoked exactly once
  * webhook verification (GET) with the wrong token → 403
  * WHATSAPP_ALLOWED_PHONE → a stranger's correctly signed message is dropped
  * malformed JSON / non-text payloads → acked or rejected, agent never invoked

The handler is a stdlib BaseHTTPRequestHandler, so the tests run it in a real
ThreadingHTTPServer bound to 127.0.0.1:0 — loopback only. Waku and the Meta
Cloud API send are stubbed; the HTTP round-trips are real. No Meta credentials,
no external requests, no new dependencies.

WHATSAPP_ALLOWED_PHONE is read by two entry points — main() for the standalone
gateway and start_in_background() for the dashboard — but both build their
handler through _build_handler(...), so exercising that shared seam covers
both. A test against only one module-level entry point would silently cover
half of what it claims.

One timing note: do_POST acks 200 BEFORE processing messages, because Meta
disables webhooks that respond slowly. The handler speaks HTTP/1.0 and sends
no Content-Length, so the body is delimited by connection close — a client
that has read the full response body knows do_POST has returned and every
respond() call (or deliberate skip) has already happened. That is what makes
the assertions below exact instead of racy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from waku.gateway import whatsapp

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ALLOWED_PHONE = "15550001111"
STRANGER_PHONE = "15559998888"


class _FakeWaku:
    """Stands in for the agent: records turns instead of running the loop."""

    def __init__(self, **_kwargs) -> None:
        self.session = SimpleNamespace(session_id="")
        self.calls: list[dict] = []

    def respond(self, text, *, observer=None, source=None):
        self.calls.append({"text": text, "source": source})
        return SimpleNamespace(reply=f"echo: {text}")


class _FakeSettings:
    home = Path("/nonexistent-waku-home")

    def ensure_home(self) -> None:
        pass


@pytest.fixture()
def gateway(monkeypatch):
    """The real handler, served on loopback, with the agent + Meta API stubbed."""
    built: list[_FakeWaku] = []
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(whatsapp, "Waku", lambda **kw: built.append(_FakeWaku()) or built[-1])
    monkeypatch.setattr("waku.config.load_settings", lambda: _FakeSettings())
    monkeypatch.setattr("waku.db.connect", lambda *a, **k: None)
    monkeypatch.setattr(
        whatsapp, "_send_message", lambda _t, _p, to, text: sent.append((to, text)) or True
    )

    handler = whatsapp._build_handler("token", "phone-number-id", VERIFY_TOKEN, APP_SECRET,
                                      ALLOWED_PHONE)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{httpd.server_address[1]}/webhook", waku=built[0], sent=sent
    )
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(*, sender: str = ALLOWED_PHONE, text: str | None = "hello waku") -> bytes:
    """A Meta webhook body. text=None produces a non-text (image) message."""
    message = {"from": sender, "id": "wamid.test", "type": "text"}
    if text is not None:
        message["text"] = {"body": text}
    else:
        message["type"] = "image"
        message["image"] = {"id": "media-1"}
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "e1", "changes": [{"field": "messages",
                                            "value": {"messages": [message]}}]}],
    }).encode()


def _post(gateway, body: bytes, signature: str | None) -> int:
    """POST /webhook. signature=None means the header is absent entirely."""
    headers = {} if signature is None else {"X-Hub-Signature-256": signature}
    req = urllib.request.Request(gateway.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()  # close-delimited body: reading it means do_POST returned
            return resp.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def _get_verify(gateway, token: str) -> tuple[int, str]:
    query = urllib.parse.urlencode(
        {"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "challenge-1"}
    )
    req = urllib.request.Request(f"{gateway.url}?{query}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---------- the signature boundary (PR #26's regression test)


def test_missing_signature_is_403_and_never_reaches_the_agent(gateway):
    """No X-Hub-Signature-256 header at all — a request that never saw the
    secret must be turned away before the payload is even parsed."""
    body = _payload()
    assert _post(gateway, body, signature=None) == 403
    assert gateway.waku.calls == []
    assert gateway.sent == []


def test_forged_signature_is_403_and_never_reaches_the_agent(gateway):
    """THE regression PR #26 asked for: an attacker who finds the webhook URL
    and signs with the wrong secret gets a 403, and waku.respond() never runs —
    without it, anyone can drive the agent (and its memory) from their phone."""
    body = _payload()
    assert _post(gateway, body, signature=_sign(body, secret="not-the-app-secret")) == 403
    assert gateway.waku.calls == []
    assert gateway.sent == []


def test_correctly_signed_text_message_is_200_and_invokes_the_agent_once(gateway):
    """The happy path has to keep working, or the guard would be 'secure' the
    way a door nailed shut is secure. One message → one turn → one reply sent
    back through the (stubbed) Cloud API."""
    body = _payload(text="what is on my calendar today?")
    assert _post(gateway, body, signature=_sign(body)) == 200
    assert gateway.waku.calls == [
        {"text": "what is on my calendar today?", "source": "whatsapp"}
    ]
    assert gateway.sent == [(ALLOWED_PHONE, "echo: what is on my calendar today?")]


# ---------- webhook verification (GET)


def test_verification_with_the_wrong_token_is_403(gateway):
    status, _ = _get_verify(gateway, token="not-the-verify-token")
    assert status == 403


def test_verification_with_the_configured_token_echoes_the_challenge(gateway):
    """The negative test above is only meaningful if a correct token still
    passes — a handler that 403s everything would pass it too."""
    status, body = _get_verify(gateway, token=VERIFY_TOKEN)
    assert status == 200
    assert body == "challenge-1"


# ---------- who may talk to the agent


def test_allowed_phone_drops_a_strangers_signed_message(gateway):
    """WHATSAPP_ALLOWED_PHONE gates WHO may drive the agent, not WHETHER the
    webhook acks: Meta must still get its fast 200 (or it disables the
    webhook), but the stranger's turn and reply must never happen."""
    body = _payload(sender=STRANGER_PHONE)
    assert _post(gateway, body, signature=_sign(body)) == 200
    assert gateway.waku.calls == []
    assert gateway.sent == []


# ---------- payloads that are not agent turns


def test_malformed_json_is_400_and_never_reaches_the_agent(gateway):
    body = b"this is not json"
    assert _post(gateway, body, signature=_sign(body)) == 400
    assert gateway.waku.calls == []
    assert gateway.sent == []


def test_non_text_messages_are_acked_but_never_reach_the_agent(gateway):
    """Images, audio, stickers: correctly signed and from the allowed phone, so
    Meta gets its 200 — but there is no text to turn into an agent turn."""
    body = _payload(text=None)
    assert _post(gateway, body, signature=_sign(body)) == 200
    assert gateway.waku.calls == []
    assert gateway.sent == []
