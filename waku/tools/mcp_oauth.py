"""OAuth for remote MCP servers — the browser flow, and where the tokens live.

A remote server can be authorised two ways. `auth_env` names an environment
variable holding a long-lived key: simple, but somebody has to issue the key
and hand it over, which does not survive contact with a stranger. This module
is the other way — the one MCP's own authorization spec describes. The user
runs waku, a browser opens, they sign in on the server's own page, and no
secret ever passes through a human's clipboard or a config file.

Nothing here is waku-specific. It is the standard flow: discover the
authorization server from the resource's metadata, register this client
dynamically, open the browser, catch the redirect on loopback, exchange the
code. The SDK does all of it; this file supplies the three things the SDK
cannot know — where to keep the tokens, how to open a browser, and how to
catch the callback.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

# Where a server's tokens and its dynamic registration are kept. One file per
# server rather than one shared file: two servers' registrations have nothing
# to do with each other, and a corrupt file should cost one connection rather
# than all of them.
AUTH_DIR = "mcp-auth"

# The loopback port the authorization server redirects back to. Fixed rather
# than ephemeral because the redirect URI is registered with the authorization
# server at DCR time and has to still be true on the next run — a random port
# would force a re-registration every time, and some servers pin the URI.
CALLBACK_PORT = 41765
CALLBACK_PATH = "/callback"


def _sanitise(name: str) -> str:
    """A server name is user-supplied and becomes a filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:64]


class FileTokenStorage(TokenStorage):
    """Tokens and client registration on disk, one JSON file per server.

    Written 0600 and never logged. This file is a bearer credential: anything
    holding it can act as the user against that server until it expires.
    """

    def __init__(self, home: Path, server_name: str) -> None:
        self._path = Path(home) / AUTH_DIR / f"{_sanitise(server_name)}.json"

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            # A truncated or hand-edited file is treated as absent rather than
            # fatal: the cost is one more sign-in, and the alternative is a
            # harness that will not start until someone deletes a file whose
            # name they do not know.
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted write cannot leave a half file
        # where a valid one was, and chmod before the rename so the secret is
        # never briefly world-readable.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    async def get_tokens(self):
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self):
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the one redirect, shows a page, and hands the code back.

    `result` is deliberately class-level: BaseHTTPRequestHandler is
    instantiated by the server per request, so an instance attribute would be
    thrown away with the instance that set it. Only ever one sign-in is in
    flight, and `callback_handler` clears it before starting the server.
    """

    result: ClassVar[dict] = {}

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        body = (
            b"<html><body style='font-family:system-ui;padding:3rem'>"
            b"<h2>Signed in.</h2><p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence: this server lives for one request inside a chat session."""


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def build_provider(server_url: str, server_name: str, home: Path) -> OAuthClientProvider:
    """The provider the HTTP client authenticates with.

    `OAuthClientProvider` is an httpx auth flow, so it attaches to the client
    rather than to a request: the SDK retries the 401 itself, discovers the
    authorization server from the resource metadata, and only then calls the
    two handlers below.
    """
    redirect_uri = f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"

    metadata = OAuthClientMetadata(
        client_name="Waku",
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )

    async def redirect_handler(authorization_url: str) -> None:
        print("\n  Opening your browser to sign in…")
        print(f"  If it does not open: {authorization_url}\n")
        # `open_new_tab` rather than `open`: a headless or SSH session has no
        # browser, and this returning False is the honest signal that the URL
        # printed above is the only way through.
        webbrowser.open_new_tab(authorization_url)

    async def callback_handler() -> AuthorizationCodeResult:
        if not _port_is_free(CALLBACK_PORT):
            raise RuntimeError(
                f"port {CALLBACK_PORT} is in use — the sign-in redirect has nowhere to land. "
                "Close whatever holds it and try again."
            )
        _CallbackHandler.result = {}
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        # Poll rather than join: this runs on the bridge's event loop, and a
        # blocking join would stop the loop the transport is about to need.
        for _ in range(600):  # 5 minutes at 0.5s
            if _CallbackHandler.result:
                break
            await asyncio.sleep(0.5)
        server.server_close()

        result = _CallbackHandler.result
        if not result:
            raise RuntimeError("timed out waiting for the browser sign-in to come back")
        if "error" in result:
            # The server said no. Surface its own words: "access_denied" and
            # "invalid_client" send you to completely different places.
            raise RuntimeError(
                f"authorization failed: {result['error']}"
                + (f" — {result['error_description']}" if "error_description" in result else "")
            )
        return AuthorizationCodeResult(
            code=result["code"], state=result.get("state"), iss=result.get("iss")
        )

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=FileTokenStorage(home, server_name),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
