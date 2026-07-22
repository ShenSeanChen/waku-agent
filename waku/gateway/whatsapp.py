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

  4. Set up webhook verification (WHATSAPP_VERIFY_TOKEN):
     - Pick any random string, e.g. "my-waku-verify-token-123"
     - You'll enter this in Meta's webhook config later

  5. Expose your local server publicly:
     - Install ngrok: https://ngrok.com (free tier gives you a URL)
     - Run: ngrok http 5000
     - Copy the https://xxxx.ngrok.io URL

  6. Configure the webhook in Meta's dashboard:
     - Go to your app → WhatsApp → Configuration → Webhook
     - Callback URL: https://xxxx.ngrok.io/webhook
     - Verify token: the string you chose in step 4
     - Subscribe to "messages" field

  7. Set env vars in .env:
     WHATSAPP_TOKEN=your_access_token
     WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
     WHATSAPP_VERIFY_TOKEN=your_random_verify_string

  8. Run: make whatsapp

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

  - METa'S DASHBOARD IS A MAZE. Settings live acrossdevelopers.facebook.com,
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

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from waku.app import Waku
from waku.gateway.cli import _observer  # mirror gate/tool activity on the laptop terminal


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


def _build_handler(token: str, phone_number_id: str, verify_token: str, allowed: str):
    """Build the HTTP request handler with captured config. Shared by the
    standalone gateway and the background server the dashboard starts."""

    waku = Waku()
    waku.session.session_id = "whatsapp"  # its own conversation thread in the inbox

    class Handler(BaseHTTPRequestHandler):
        def _set_json(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

        # ── GET /webhook — Meta's one-time verification challenge ──────
        def do_GET(self) -> None:
            from urllib.parse import urlparse, parse_qs

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
            from urllib.parse import urlparse

            parsed = urlparse(self.path)
            if parsed.path != "/webhook":
                self._set_json(404)
                self.wfile.write(b"{}")
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            try:
                import json

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
                        result = waku.respond(
                            text, observer=_observer, source="whatsapp"
                        )
                        print(f"waku › {result.reply}")

                        _send_message(token, phone_number_id, sender, result.reply or "(no reply)")

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

    handler = _build_handler(token, phone_number_id, verify_token, allowed)
    server = ThreadingHTTPServer(("0.0.0.0", 5000), handler)
    print("WhatsApp gateway → listening on http://0.0.0.0:5000")
    print("  Webhook URL: http://<your-public-url>/webhook")
    print("  Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def start_in_background() -> bool:
    """Start the WhatsApp webhook server on a daemon thread — so
    `waku dashboard` runs the browser cockpit AND WhatsApp from one command.
    Returns True if started, False (quietly) if tokens aren't set or the
    extra isn't installed. Never raises: a gateway problem must not take
    down the dashboard."""
    from waku.config import load_settings

    settings = load_settings()
    token = settings.whatsapp_token
    phone_number_id = settings.whatsapp_phone_number_id
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")

    if not token or not phone_number_id or not verify_token:
        return False
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("(whatsapp) WHATSAPP_TOKEN is set but the extra isn't installed — "
              "pip install 'waku-agent[whatsapp]'")
        return False

    allowed = os.getenv("WHATSAPP_ALLOWED_PHONE", "")

    def run() -> None:
        try:
            handler = _build_handler(token, phone_number_id, verify_token, allowed)
            server = ThreadingHTTPServer(("0.0.0.0", 5000), handler)
            server.serve_forever()
        except Exception as exc:  # noqa: BLE001 — isolate the dashboard from gateway errors
            print(f"(whatsapp) background server stopped: {exc}")

    threading.Thread(target=run, daemon=True, name="whatsapp-webhook").start()
    return True


if __name__ == "__main__":
    main()
