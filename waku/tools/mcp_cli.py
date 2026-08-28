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
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

WAKU_HOME = Path(".waku")


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
    return f"{who} — {int(left.total_seconds() // 3600)}h left"


def _servers(home: Path) -> list[dict]:
    config = home / "mcp.json"
    if not config.exists():
        return []
    try:
        return json.loads(config.read_text(encoding="utf-8")).get("servers", [])
    except json.JSONDecodeError as exc:
        print(f"{config} is not valid JSON: {exc}")
        raise SystemExit(1) from exc


def _auth_file(home: Path, name: str) -> Path:
    from waku.tools.mcp_oauth import FileTokenStorage

    return FileTokenStorage(home, name)._path


def _list(home: Path) -> int:
    servers = _servers(home)
    if not servers:
        print(f"No MCP servers configured. See docs/integrations.md, or write {home}/mcp.json")
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
    """Forget the token, then connect — which is what triggers a sign-in.

    Signing out first is the whole point: without it the stored token is still
    valid and the server never asks who you are, so "log in as someone else"
    would silently keep the account you were trying to leave.
    """
    names = [s["name"] for s in _servers(home)]
    if name not in names:
        print(f"No server called '{name}' in {home}/mcp.json. Configured: {', '.join(names) or 'none'}")
        return 1

    path = _auth_file(home, name)
    if path.exists():
        path.unlink()

    from waku.tools.mcp_client import MCPBridge

    bridge = MCPBridge(home / "mcp.json")
    try:
        bridge.start()
    finally:
        bridge.close()

    print(f"\n  {name} — {_identity(path)}")
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[2:] if argv is None else argv)
    home = Path(WAKU_HOME)

    if not args:
        return _list(home)
    if args[0] in {"login", "logout"} and len(args) == 2:
        return (_login if args[0] == "login" else _logout)(home, args[1])

    print(__doc__.split("    waku mcp ", 1)[0].strip())
    print("\n    waku mcp\n    waku mcp login <name>\n    waku mcp logout <name>")
    return 1
