"""WhatsApp gateway — message your laptop from your phone via WhatsApp.

Setup (10-20 minutes, free tier available but painful):

  1. Create a Meta developer account at https://developers.facebook.com
     - You MUST use a real phone number for verification. Meta's setup is
       notoriously hostile to hobbyists: expect 2FA, business verification
       prompts, and review processes that can take days. The free tier only
     lets you message numbers you've added as "test users" in the app dashboard.

  2. Create a WhatsApp Business app:
     - Go to https://developers.facebook.com/apps → "Create App" → "Business"
     - Add the "WhatsApp" product to your new app
     - Note your Phone Number ID from the WhatsApp > Getting Started page

  3. Get your access token (WHATSAPP_TOKEN):
     - In the WhatsApp > Getting Started page, generate a temporary token
     - For production, create a System User at https://business.facebook.com/settings
       and generate a permanent token with whatsapp_business_messaging permission
     - Temporary tokens expire after 24 hours — set up a permanent one early

  4. Get your App Secret (WHATSAPP_APP_SECRET):
     - Go to your app → Settings → Basic → App Secret
     - This is used to verify that incoming webhooks are actually from Meta
     - Without this, anyone who finds your webhook URL can forge requests

  5. Set up webhook verification (WHATSAPP_VERIFY_TOKEN):
     - Pick any random string, e.g. "my-waku-verify-token-123"
     - You'll enter this in Meta's webhook config later

  6. Expose your local server publicly:
     - Install ngrok: https://ngrok.com (free tier gives you a URL)
     - Run: ngrok http 5000
     - Copy the https://xxxx.ngrok.io URL

  7. Configure the webhook in Meta's dashboard:
     - Go to your app → WhatsApp → Configuration → Webhook
     - Callback URL: https://xxxx.ngrok.io/webhook
     - Verify token: the string you chose in step 5
     - Subscribe to "messages" field

  8. Set env vars in .env:
     WHATSAPP_TOKEN=your_access_token
     WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
     WHATSAPP_APP_SECRET=your_app_secret
     WHATSAPP_VERIFY_TOKEN=your_random_verify_string

  9. Run: make whatsapp

  Common gotchas and pain points (Meta's setup is a rite of passage):

  - TEMPORARY TOKENS EXPIRE IN 24 HOURS. Your bot will silently stop working
    until you regenerate the token. Create a permanent System User token ASAP.

  - FREE TIER IS SANDBOX-ONLY. You can only message phone numbers you've added
    as test recipients in the app dashboard (WhatsApp > Getting Started >
    "To" field). Random people can't message your bot until you pass business
    verification, which Meta may reject or ignore for months.

  - BUSINESS VERIFICATION IS SEPARATE from app review. You need both:
    (a) your Meta business account verified, AND (b) your WhatsApp use case
    approved. Neither is fast or guaranteed.

  - NGROK FREE URLS CHANGE EVERY RESTART. Every time you restart ngrok, you
    get a new URL and must reconfigure the webhook in Meta's dashboard.
    Ngrok paid plans give you a stable subdomain.

  - META'S DASHBOARD IS A MAZE. Settings live across developers.facebook.com,
    business.facebook.com, and the WhatsApp Manager at business.whatsapp.com.
    Bookmark all three.

  - WEBHOOK VERIFICATION IS ONE-TIME. Meta calls your /webhook GET endpoint
    once to verify, then never again. If your server restarts, the webhook
    stays configured — but if you change the verify token, you must re-save
    the webhook config in Meta's dashboard.

  - RATE LIMITS: Meta caps at ~80 messages/second on the free tier and will
    throttle or block your app if you exceed it. The 429 responses are not
    always graceful.

  - MESSAGE WINDOW: Meta only lets you reply to incoming messages within a
    24-hour "customer service window". After that, you can only send pre-
    approved template messages (which cost per message on the free tier).

  - PHONE NUMBER FORMAT: WhatsApp sends phone numbers without '+' prefix
    (e.g. "15551234567"). The gateway stores them as-is.

  Despite all this, once it works, WhatsApp is the most natural mobile
  gateway — everyone already has it. Just budget an afternoon for setup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from waku.app import Waku
from waku.gateway.cli import _observer  # mirror gate/tool activity on the laptop terminal
from waku.integrations import IntegrationState, IntegrationStatus


def _send_message(token: str, phone_number_id: str, to: str, text: str) -> bool:
    """Send a text message via the Meta Cloud API. Returns True on success."""
    import httpx

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"(whatsapp) send failed: {exc}")
        return False


def _build_handler(
    token: str,
    phone_number_id: str,
    verify_token: str,
    app_secret: str,
    allowed: str,
):
    """Build the HTTP request handler with captured config. Shared by the
    standalone gateway and the background server the dashboard starts.

    ThreadingHTTPServer dispatches requests on worker threads, so we pass
    ``check_same_thread=False`` and use a lock to serialise turns through
    the same Waku instance."""

    from waku.config import load_settings
    from waku.db import connect

    s = load_settings()
    s.ensure_home()
    waku = Waku(settings=s, conn=connect(s.home, check_same_thread=False))
    waku.session.session_id = "whatsapp"
    waku_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _set_json(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

        # ── GET /webhook — Meta's one-time verification challenge ──────
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/webhook":
                self._set_json(404)
                self.wfile.write(b"{}")
                return

            params = parse_qs(parsed.query)
            mode = params.get("hub.mode", [None])[0]
            challenge = params.get("hub.challenge", [None])[0]
            incoming_token = params.get("hub.verify_token", [None])[0]

            if mode == "subscribe" and incoming_token == verify_token and challenge:
                print("(whatsapp) webhook verified")
                self._set_json(200)
                self.wfile.write(challenge.encode())
            else:
                print(f"(whatsapp) verification failed: mode={mode} token={incoming_token}")
                self._set_json(403)
                self.wfile.write(b"forbidden")

        # ── POST /webhook — incoming messages from WhatsApp ────────────
        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/webhook":
                self._set_json(404)
                self.wfile.write(b"{}")
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            # Verify Meta's X-Hub-Signature-256 — without this, anyone who
            # finds the webhook URL can forge payloads and drive the agent.
            sig = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                app_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                print("(whatsapp) rejected: bad signature")
                self._set_json(403)
                self.wfile.write(b"{}")
                return

            try:
                data = json.loads(body)
            except Exception:
                self._set_json(400)
                self.wfile.write(b"bad json")
                return

            # Always 200 fast — Meta retries on timeouts and will disable
            # your webhook after enough failures.
            self._set_json(200)
            self.wfile.write(b"{}")

            # Parse the webhook payload
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        sender = msg.get("from", "")
                        text = msg.get("text", {}).get("body", "")

                        if not text or not sender:
                            continue
                        if allowed and sender != allowed:
                            print(f"(whatsapp) rejected message from {sender} (not allowed)")
                            continue

                        print(f"you › [{sender}] {text}")
                        with waku_lock:
                            result = waku.respond(
                                text, observer=_observer, source="whatsapp"
                            )
                        print(f"waku › {result.reply}")

                        _send_message(
                            token, phone_number_id, sender, result.reply or "(no reply)"
                        )

    return Handler


def main() -> None:
    try:
        import httpx  # noqa: F401
    except ImportError:
        raise SystemExit("WhatsApp extra not installed: pip install 'waku-agent[whatsapp]'")

    from waku.config import load_settings

    settings = load_settings()
    token = settings.whatsapp_token
    phone_number_id = settings.whatsapp_phone_number_id
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "")
    allowed = os.getenv("WHATSAPP_ALLOWED_PHONE", "")

    if not token:
        raise SystemExit(
            "Set WHATSAPP_TOKEN in .env (Meta Cloud API access token). "
            "See waku/gateway/whatsapp.py docstring for setup instructions."
        )
    if not phone_number_id:
        raise SystemExit(
            "Set WHATSAPP_PHONE_NUMBER_ID in .env (from WhatsApp > Getting Started page)."
        )
    if not verify_token:
        raise SystemExit(
            "Set WHATSAPP_VERIFY_TOKEN in .env (any random string you'll enter in Meta's dashboard)."
        )
    if not app_secret:
        raise SystemExit(
            "Set WHATSAPP_APP_SECRET in .env (from your app's Settings → Basic → App Secret). "
            "This is required to verify webhook signatures — without it, anyone who finds "
            "your webhook URL can forge requests and drive the agent."
        )

    handler = _build_handler(token, phone_number_id, verify_token, app_secret, allowed)
    server = ThreadingHTTPServer(("0.0.0.0", 5000), handler)
    print("WhatsApp gateway → listening on http://0.0.0.0:5000")
    print("  Webhook URL: http://<your-public-url>/webhook")
    print("  Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


class WhatsAppHandle:
    """Supervisor contract for the background webhook server — same shape as
    TelegramHandle/DiscordHandle so GatewaySupervisor can stop, restart and
    report it like any other gateway."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self.server, self.thread = server, thread

    def stop(self) -> None:
        # shutdown() must be called from a thread OTHER than the one running
        # serve_forever, or it deadlocks — the supervisor always calls from
        # the dashboard thread, so this is safe.
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=10)

    def status(self) -> IntegrationStatus:
        if not self.thread.is_alive():
            return IntegrationStatus(IntegrationState.ERROR, "gateway stopped unexpectedly")
        return IntegrationStatus(IntegrationState.CONNECTED)


def start_in_background() -> WhatsAppHandle | None:
    """Start the WhatsApp webhook server on a daemon thread — so
    `waku dashboard` runs the browser cockpit AND WhatsApp from one command.
    Returns a handle the supervisor can stop/status, None (quietly) if tokens
    aren't set or the extra isn't installed. A bind failure (e.g. port 5000
    busy) raises — the supervisor reports it as the gateway's error status."""
    from waku.config import load_settings

    settings = load_settings()
    token = settings.whatsapp_token
    phone_number_id = settings.whatsapp_phone_number_id
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "")

    if not token or not phone_number_id or not verify_token or not app_secret:
        return None
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("(whatsapp) WHATSAPP_TOKEN is set but the extra isn't installed — "
              "pip install 'waku-agent[whatsapp]'")
        return None

    allowed = os.getenv("WHATSAPP_ALLOWED_PHONE", "")
    handler = _build_handler(token, phone_number_id, verify_token, app_secret, allowed)
    server = ThreadingHTTPServer(("0.0.0.0", 5000), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="whatsapp-webhook")
    thread.start()
    return WhatsAppHandle(server, thread)


if __name__ == "__main__":
    main()
