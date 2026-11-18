"""`waku mcp` — which servers are configured, and who you are signed in as.

Two agents pointed at the same memory server, signed in as two different
people, look exactly like a broken server: you write something in one and the
other cannot find it. That happened, and the reason it cost an afternoon is
that nothing anywhere printed the account. The token was on disk the whole
time with the email inside it.

So this command's first job is not switching accounts, it is *showing* them.
Switching is the easy part once you can see there is something to switch.

    waku mcp                 what is configured, and who each server knows you as
    waku mcp login <name>    sign in again — as someone else, or after expiry
    waku mcp logout <name>   forget the token; the next run signs in fresh

Adding a server is one command (spec 025), and it writes the entry in
mcp.json that this file reads:

    waku mcp add <name> <https address>                a remote server, signed in to in your browser
    waku mcp add <name> <https address> --key-env VAR  a remote server whose key is in $VAR
    waku mcp add <name> <https address> --no-auth      a remote server with no sign-in
    waku mcp add <name> -- <command> [arguments]       a server this machine runs
    waku mcp remove <name>                             delete the entry and its sign-in
"""

from __future__ import annotations

import base64
import json
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path


def _claims(access_token: str) -> dict:
    """The token's own claims, read without verifying the signature.

    Deliberately unverified: this is our own stored credential being described
    back to its owner, not a token being trusted for access. Verification is
    the server's job and it does it on every call. Doing it here would mean
    shipping the server's public key to display an email address.
    """
    if access_token.count(".") != 2:
        return {}
    body = access_token.split(".")[1]
    body += "=" * (-len(body) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:
        return {}


def _identity(auth_file: Path) -> str:
    """One line describing whose token this is, or why there is not one."""
    if not auth_file.exists():
        return "not signed in"
    try:
        stored = json.loads(auth_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Same treatment as the storage layer gives it: a file that will not
        # parse costs a sign-in, not an explanation.
        return "unreadable — will sign in again"

    claims = _claims(stored.get("tokens", {}).get("access_token", ""))
    who = claims.get("email") or claims.get("sub") or "unknown account"

    exp = claims.get("exp")
    if not exp:
        return who
    left = datetime.fromtimestamp(exp, UTC) - datetime.now(UTC)
    if left.total_seconds() <= 0:
        # Worth saying rather than hiding: an expired token is refreshed
        # silently on the next call, and someone reading this should not
        # mistake the refresh for a problem.
        return f"{who} — token expired, refreshes on next use"
    # Minutes below an hour: a token minted fifty minutes ago rendered as
    # "0h left", which reads as expired and is the opposite of the truth.
    minutes = int(left.total_seconds() // 60)
    return f"{who} — {minutes // 60}h left" if minutes >= 60 else f"{who} — {minutes}m left"


def _servers(home: Path) -> list[dict]:
    """mcp.json's servers and the ones installed plugins bring (spec 021),
    merged the way the bridge merges them."""
    from waku.plugins import store
    from waku.tools.mcp_client import merge

    config = home / "mcp.json"
    servers = []
    if config.exists():
        try:
            servers = json.loads(config.read_text(encoding="utf-8")).get("servers", [])
        except json.JSONDecodeError as exc:
            print(f"{config} is not valid JSON: {exc}")
            raise SystemExit(1) from exc
    return merge(servers, store.servers(home))[0]


def _auth_file(home: Path, name: str) -> Path:
    from waku.tools.mcp_oauth import FileTokenStorage

    return FileTokenStorage(home, name)._path


def _list(home: Path) -> int:
    servers = _servers(home)
    if not servers:
        print("No MCP servers configured. Add one: waku mcp add <name> <https address>")
        return 0

    for spec in servers:
        name = spec["name"]
        if spec.get("url"):
            if spec.get("oauth"):
                auth = f"oauth · {_identity(_auth_file(home, name))}"
            elif spec.get("auth_env"):
                auth = f"api key · ${spec['auth_env']}"
            else:
                auth = "no credential"
            print(f"  {name}\n    {spec['url']}\n    {auth}")
        else:
            print(f"  {name}\n    {spec['command']} (local process)\n    no credential needed")
    return 0


def _logout(home: Path, name: str) -> int:
    path = _auth_file(home, name)
    if not path.exists():
        print(f"'{name}' is not signed in")
        return 0
    path.unlink()
    print(f"Signed out of '{name}'. The next run will sign in again.")
    return 0


def _login(home: Path, name: str) -> int:
    ok, message = sign_in(home, name)
    print(message)
    return 0 if ok else 1


def sign_in(home: Path, name: str) -> tuple[bool, str]:
    """Forget the token, then connect — which is what triggers a sign-in.

    Signing out first is the whole point: without it the stored token is still
    valid and the server never asks who you are, so "log in as someone else"
    would silently keep the account you were trying to leave.

    Returns whether a token now exists, and the line to show. `waku connect
    waku-memory` shows that line in the dashboard chat, where a print would
    land in the server log instead.
    """
    names = [s["name"] for s in _servers(home)]
    if name not in names:
        return False, f"No server called '{name}' in {home}/mcp.json. Configured: {', '.join(names) or 'none'}"

    path = _auth_file(home, name)
    if path.exists():
        path.unlink()

    from waku.plugins import store
    from waku.tools.mcp_client import MCPBridge

    # Success here is "a token now exists for the account you chose", not "a
    # session was established". They came apart in practice: the sign-in
    # completed, the token was written, and the reconnect on the same
    # already-failed exit stack reported an error anyway — so the command said
    # it failed immediately after succeeding. This command exists to sign in;
    # holding a session is the next run's job.
    bridge = MCPBridge(home / "mcp.json", extra_servers=store.servers(home))
    try:
        bridge.start()
    except TimeoutError:
        # A traceback here is the wrong answer to "you took too long in the
        # browser". The sign-in may still have completed — the callback writes
        # the token whether or not anyone is still waiting — so say what to
        # check rather than what broke.
        return False, ("\n  Timed out waiting for the browser sign-in.\n"
                       "  If you did finish it, `waku mcp` will show the account. Otherwise run this again.")
    except Exception:
        # Swallowed deliberately, and only here: the token below is the thing
        # that was asked for, and it is either on disk or it is not. A
        # connection error after a successful sign-in is noise the next run
        # will not reproduce.
        pass
    finally:
        bridge.close()

    if not path.exists():
        return False, f"\n  Sign-in did not complete — '{name}' has no token."
    return True, f"\n  {name} — {_identity(path)}"


# What opens the sign-in link on this machine. A name of its own so an eval
# replaces it and no test ever opens a browser.
_open_browser = webbrowser.open_new_tab

ADD_USAGE = __doc__.split("mcp.json that this file reads:", 1)[1].strip("\n")


def _add(home: Path, args: list[str]) -> int:
    """`waku mcp add`: write the entry, then sign in when the server signs in
    on its own page."""
    from waku.tools import connectors
    from waku.tools.mcp_oauth import SIGN_IN_TIMEOUT

    command: list[str] = []
    if "--" in args:
        cut = args.index("--")
        args, command = args[:cut], args[cut + 1:]
    auth = None
    if "--key-env" in args:
        at = args.index("--key-env")
        if at + 1 >= len(args):
            print(ADD_USAGE)
            return 1
        auth = args[at + 1]
        del args[at:at + 2]
    if "--no-auth" in args:
        args.remove("--no-auth")
        auth = connectors.NONE
    # Group B resolves a lone name from the starter list; until a name is on
    # it, an address or a command has to come with the name.
    if not args or len(args) > 2 or (len(args) == 2 and command) or (
            len(args) == 1 and not command):
        print(ADD_USAGE)
        return 1
    try:
        added = connectors.add(home, args[0], url=args[1] if len(args) == 2 else None,
                               command=command[0] if command else None,
                               args=command[1:] or None, auth=auth)
    except connectors.Refused as refused:
        print(refused)
        return 1
    print(f"Added '{added.name}' to {home / 'mcp.json'}.")
    if added.auth != connectors.OAUTH:
        print(added.next)
        return 0

    try:
        flow = connectors.start_sign_in(home, added.name)
    except connectors.Refused as refused:
        print(f"{refused}\n{added.next}")
        return 0
    if flow.link:
        print(f"Sign in here:\n  {flow.link}\nWaiting for you to finish in the browser.")
        _open_browser(flow.link)
    if not flow.wait(SIGN_IN_TIMEOUT + 60.0) or not flow.ok:
        print(f"'{added.name}' is added and not signed in yet. {added.next}")
        return 0
    print(connectors.connected_line(added.name, flow.tools or []))
    print("Restart Waku to load its tools.")
    return 0


def _remove(home: Path, name: str) -> int:
    from waku.tools import connectors

    try:
        connectors.remove(home, name)
    except connectors.Refused as refused:
        print(refused)
        return 1
    print(f"Removed '{name}' and its sign-in. Restart Waku to drop its tools.")
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[2:] if argv is None else argv)
    from waku.config import load_settings

    home = load_settings().home   # the same home as every other command

    if not args:
        return _list(home)
    if args[0] in {"login", "logout"} and len(args) == 2:
        return (_login if args[0] == "login" else _logout)(home, args[1])
    if args[0] == "add":
        return _add(home, args[1:])
    if args[0] == "remove" and len(args) == 2:
        return _remove(home, args[1])

    print(__doc__.split("    waku mcp ", 1)[0].strip())
    print("\n    waku mcp\n    waku mcp login <name>\n    waku mcp logout <name>")
    print(ADD_USAGE)
    return 1
