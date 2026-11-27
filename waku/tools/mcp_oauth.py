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

Keeping a sign-in alive is the part the SDK gets wrong on its own, and
`_FileSharedProvider` below is the fix. An access token lasts an hour and a
refresh token renews it. The SDK records when a token expires only in memory,
at the moment it receives the token. A process that loads the token from the
file again does not know when it was issued, so the SDK treats it as valid
forever: it sends it expired, gets a 401, and answers the 401 with a full
sign-in, never a refresh. A chat turn refuses a sign-in (spec 038), so on
2026-11-25 Waku Memory said "needs a sign-in" a few hours after a fresh one,
and the token file was never written again. The file now records the expiry
time, and the provider reads it back.

Several Waku processes share one token file: two dashboards, `waku serve`, a
script. Waku Memory's authorization server rotates the refresh token on every
refresh and refuses the old one, so two processes must not spend the same
refresh token. The provider takes a lock on the file around a refresh, reads
the file again first, and uses tokens another process already refreshed.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

try:
    import fcntl
except ImportError:  # Windows has no fcntl: the refused-refresh re-read still helps
    fcntl = None

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
# A first sign-in, which has no registration yet, takes another free port when
# this one is busy (`callback_port`), and registers that one.
CALLBACK_PORT = 41765
CALLBACK_PATH = "/callback"

# How long the callback server waits for the browser to come back. This is
# human time — reading a consent screen, picking between two Google accounts,
# or walking from the phone that got the link to the computer that can open
# it — so it is minutes, not seconds. Exported because the caller that waits
# on the connection has to wait longer than this, and two independently-chosen
# numbers is how the CLI came to give up at 60s while this sat patiently.
# Every sentence that tells the person how long a link lasts reads it through
# `sign_in_minutes`, so the words cannot drift from the wait.
SIGN_IN_TIMEOUT = 900.0


# A token is refreshed this many seconds before it expires, so a request that
# sets out with it does not arrive at the server just after it lapsed.
EXPIRY_MARGIN = 60.0

# How long a process waits for another one to finish its refresh. A refresh is
# one HTTP request; after this long the holder is presumed stuck, and this
# process refreshes without the lock rather than stop the turn.
LOCK_TIMEOUT = 30.0


def sign_in_minutes() -> int:
    """How long a sign-in link works, in whole minutes, for the person."""
    return max(1, round(SIGN_IN_TIMEOUT / 60))


def _sanitise(name: str) -> str:
    """A server name is user-supplied and becomes a filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:64]


class FileTokenStorage(TokenStorage):
    """Tokens and client registration on disk, one JSON file per server.

    Written 0600 and never logged. This file is a bearer credential: anything
    holding it can act as the user against that server until it expires.
    """

    def __init__(self, home: Path, server_name: str, url: str | None = None) -> None:
        self._path = Path(home) / AUTH_DIR / f"{_sanitise(server_name)}.json"
        # Spec 021 review: a file is keyed by server id, and a plugin can name
        # any id. The file records the address it signed in to, and a server
        # at another address under the same id is never handed its tokens.
        self._url = url.rstrip("/") if url else None

    def matches(self) -> bool:
        """False when the file was written for another address. A file from
        before addresses were recorded names none, and matches."""
        stored = self._read().get("url")
        return not (self._url and stored and stored.rstrip("/") != self._url)

    def has_tokens(self) -> bool:
        """Whether a sign-in to this address finished and left a token. The
        file exists from the moment this client is registered with the
        server, which is before the person has signed in."""
        return self.matches() and bool(self._read().get("tokens"))

    def registered_port(self) -> int | None:
        """The loopback port in the redirect address this client registered
        with the server, or None when it has not registered yet."""
        if not self.matches():
            return None
        uris = (self._read().get("client_info") or {}).get("redirect_uris") or []
        try:
            return urlparse(str(uris[0])).port if uris else None
        except ValueError:
            return None

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
        return self.read_tokens()[0]

    def read_tokens(self) -> tuple[OAuthToken | None, float | None]:
        """The stored token and the moment the SDK should treat it as expired.

        The SDK computes expiry from `expires_in` when a token arrives, and
        forgets it when the process ends. `expires_at`, written beside the
        token, is what survives. A file from before `expires_at` was kept
        cannot say when its token was issued: one that can be refreshed is
        treated as expired, which costs one refresh, and one that cannot keeps
        the SDK's old reading, no expiry known.
        """
        data = self._read() if self.matches() else {}
        raw = data.get("tokens")
        if not raw:
            return None, None
        token = OAuthToken.model_validate(raw)
        if isinstance(data.get("expires_at"), (int, float)):
            return token, data["expires_at"] - EXPIRY_MARGIN
        if token.refresh_token and token.expires_in is not None:
            # Not 0: the SDK reads an expiry of 0 as "no expiry".
            return token, 1.0
        return token, None

    def _own(self) -> dict:
        """The file's data, or a fresh start when it belongs to another
        address: a new sign-in never keeps the other server's registration."""
        data = self._read() if self.matches() else {}
        if self._url:
            data["url"] = self._url
        return data

    async def set_tokens(self, tokens) -> None:
        data = self._own()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # The absolute expiry, so the next process to load this file knows it.
        data.pop("expires_at", None)
        if tokens.expires_in is not None:
            data["expires_at"] = time.time() + int(tokens.expires_in)
        self._write(data)

    async def lock(self):
        """An exclusive lock on this server's tokens across processes, or None.

        A refresh spends the refresh token, so only one process may hold one
        in flight. The lock is `flock` on a file beside the tokens, which the
        operating system releases if the holder dies. This waits on the event
        loop rather than blocking it, and gives up after LOCK_TIMEOUT."""
        if fcntl is None:
            return None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(fd)
                    return None
                await asyncio.sleep(0.05)

    @staticmethod
    def unlock(fd) -> None:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def get_client_info(self):
        if not self.matches():
            return None
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._own()
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


class PortBusy(RuntimeError):
    """The sign-in's port is held by another program and cannot change. The
    message is written for the person: it holds no address and no token."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def callback_port(storage: FileTokenStorage) -> int:
    """The port this sign-in's redirect comes back to.

    A client that is registered with the server keeps the port it registered:
    the server compares the redirect address with the one it stored, so
    another port is refused. A client that has not registered yet takes
    CALLBACK_PORT, or any free port when CALLBACK_PORT is busy, and registers
    with that one. Nothing is refused here: a server that is signed in
    connects with its token and never uses the port.
    """
    registered = storage.registered_port()
    if registered is not None:
        return registered
    return CALLBACK_PORT if _port_is_free(CALLBACK_PORT) else _free_port()


def check_port(storage: FileTokenStorage) -> None:
    """PortBusy when a sign-in cannot start: this client is registered on a
    port that another program holds, so it cannot move to another one."""
    registered = storage.registered_port()
    if registered is not None and not _port_is_free(registered):
        raise PortBusy(
            f"The sign-in cannot start: another program on the computer Waku runs on "
            f"uses port {registered}. This sign-in is registered with the server on that "
            "port, so it cannot move to another port. Close that program, or wait "
            f"{sign_in_minutes()} minutes if another sign-in is still open, and ask again.")


class SignInNeeded(RuntimeError):
    """A server asked for a sign-in while a turn was connecting (spec 038).

    A turn never opens a browser: the sign-in it would start blocks the whole
    turn for up to SIGN_IN_TIMEOUT, on a page nobody asked for. The server is
    left out of that turn instead, and the person signs in when they choose."""


class _FileSharedProvider(OAuthClientProvider):
    """The SDK's provider, made to share one token file with other processes.

    It changes three moments of the SDK's flow, and nothing else:

    - loading: the stored expiry is restored with the token, so an hour-old
      token is refreshed instead of sent;
    - before a refresh: the lock is taken and the file read again, and tokens
      another process wrote are used, refreshed only if they expired too;
    - after a refused refresh: the file is read once more, because a process
      that takes no lock (an older Waku) may have rotated the token meanwhile.

    These override private methods of `mcp`'s `OAuthClientProvider` (2.x);
    `evals/deterministic/test_mcp_oauth_refresh.py` fails if they stop being
    called.
    """

    def __init__(self, *, storage: FileTokenStorage, **kwargs) -> None:
        super().__init__(storage=storage, **kwargs)
        self._file = storage

    async def _initialize(self) -> None:
        await super()._initialize()
        self.context.token_expiry_time = self._file.read_tokens()[1]

    def _adopt_file_tokens(self) -> bool:
        """Take the file's tokens when they differ from the ones in memory.

        Tokens only move forward, so a file that differs was written by a
        newer refresh or sign-in than the one this process holds."""
        token, expiry = self._file.read_tokens()
        mine = self.context.current_tokens
        if token is None or (mine is not None and
                             (token.access_token, token.refresh_token)
                             == (mine.access_token, mine.refresh_token)):
            return False
        self.context.current_tokens = token
        self.context.token_expiry_time = expiry
        return True

    async def _handle_refresh_response(self, response) -> bool:
        if response.status_code != 200 and self._adopt_file_tokens() \
                and self.context.is_token_valid():
            return True
        return await super()._handle_refresh_response(response)

    async def _auth_flow(self, request):
        if not self._initialized:
            await self._initialize()
        held = None
        if not self.context.is_token_valid() and self.context.can_refresh_token():
            held = await self._file.lock()
            self._adopt_file_tokens()
        flow = super()._auth_flow(request)
        try:
            outgoing = await flow.__anext__()
            while True:
                if outgoing is request and held is not None:
                    # The refresh is over and written: the request itself, and
                    # any sign-in after it, run without holding other processes.
                    self._file.unlock(held)
                    held = None
                response = yield outgoing
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            self._file.unlock(held)
            await flow.aclose()


def build_provider(server_url: str, server_name: str, home: Path,
                   open_url=None, on_sign_in_needed=None) -> OAuthClientProvider:
    """The provider the HTTP client authenticates with.

    `OAuthClientProvider` is an httpx auth flow, so it attaches to the client
    rather than to a request: the SDK retries the 401 itself, discovers the
    authorization server from the resource metadata, and only then calls the
    two handlers below.

    `open_url`, when given, is handed the sign-in link in place of the
    browser this machine would open (spec 025): a chat reply or the dashboard
    shows the link, and the person opens it on the computer Waku runs on.

    `on_sign_in_needed`, when given, means a turn is connecting (spec 038): a
    sign-in is reported to it by server name and refused, and nothing opens.
    A refresh of an existing token still happens silently, since the SDK
    calls this handler only when no token can be refreshed.

    The provider is `_FileSharedProvider`, which keeps a sign-in alive across
    processes and restarts (the module docstring says why).
    """
    storage = FileTokenStorage(home, server_name, server_url)
    port = callback_port(storage)
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"

    metadata = OAuthClientMetadata(
        client_name="Waku",
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )

    async def redirect_handler(authorization_url: str) -> None:
        if on_sign_in_needed is not None:
            # A turn is connecting (spec 038): say so and stop, before any
            # browser opens or any callback port is held.
            on_sign_in_needed(server_name)
            raise SignInNeeded(f"'{server_name}' needs a sign-in")
        if open_url is not None:
            open_url(authorization_url)
            return
        print("\n  Opening your browser to sign in…")
        print(f"  If it does not open: {authorization_url}\n")
        # `open_new_tab` rather than `open`: a headless or SSH session has no
        # browser, and this returning False is the honest signal that the URL
        # printed above is the only way through.
        webbrowser.open_new_tab(authorization_url)

    async def callback_handler() -> AuthorizationCodeResult:
        check_port(storage)
        if not _port_is_free(port):
            # Another program took the port after it was chosen.
            raise PortBusy(
                f"port {port} is in use — the sign-in redirect has nowhere to land. "
                "Close whatever holds it and try again."
            )
        _CallbackHandler.result = {}
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        # Poll rather than join: this runs on the bridge's event loop, and a
        # blocking join would stop the loop the transport is about to need.
        for _ in range(int(SIGN_IN_TIMEOUT / 0.5)):
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

    return _FileSharedProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
