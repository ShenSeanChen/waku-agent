"""DETERMINISTIC EVAL — `waku mcp` says which account each server knows you as.

Two agents pointed at the same memory server, signed in as two different
people, look exactly like a broken server. That happened on 2026-08-26: a
memory written from one agent could not be found from the other, and the cause
— two accounts — was invisible because nothing printed the email that was
sitting in the stored token the whole time.

These pin the part that would have saved the afternoon: the identity line.
Nothing here touches the network or opens a browser.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
from pathlib import Path

from waku.tools.mcp_cli import _identity, _list


def _token(email: str, exp_offset: int = 3600) -> str:
    """A JWT-shaped token with a real payload and a fake signature.

    The signature is never checked by the code under test — the claims are
    being described back to their owner, not trusted for access — so a real
    one would only make the fixture harder to read.
    """
    payload = {"email": email, "sub": "u-1", "exp": int(time.time()) + exp_offset}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _auth_file(home: Path, name: str, token: str) -> Path:
    path = home / "mcp-auth" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    return path


def test_the_account_is_named_not_merely_confirmed():
    """"Signed in" is the answer that cost an afternoon. Which account is the
    question that was actually being asked."""
    home = Path(tempfile.mkdtemp())
    path = _auth_file(home, "waku_memory", _token("seanchen9832@gmail.com"))
    line = _identity(path)
    assert "seanchen9832@gmail.com" in line
    # Offsets in these fixtures sit well inside a band rather than on its edge:
    # a token created exactly one hour out is 59m by the time it is read, and a
    # test that flips on elapsed time fails for reasons that are not the code.
    assert "left" in line


def test_an_expired_token_says_so_and_says_it_is_not_a_problem():
    """An expired token refreshes silently on the next call. Showing the expiry
    without that sentence invites someone to 'fix' a working setup."""
    home = Path(tempfile.mkdtemp())
    path = _auth_file(home, "waku_memory", _token("a@b.c", exp_offset=-60))
    assert "expired" in _identity(path)
    assert "refreshes" in _identity(path)


def test_no_token_reads_as_not_signed_in_rather_than_as_an_error():
    home = Path(tempfile.mkdtemp())
    assert _identity(home / "mcp-auth" / "absent.json") == "not signed in"


def test_a_corrupt_token_file_is_described_not_raised():
    """Same contract as the storage layer: a file that will not parse costs a
    sign-in, not a stack trace."""
    home = Path(tempfile.mkdtemp())
    path = home / "mcp-auth" / "x.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert "sign in again" in _identity(path)


def test_an_api_key_server_names_the_variable_and_no_account(capsys):
    """`auth_env` servers have no account to show. Printing "not signed in"
    for one would be false — it is authorised, just not by a person."""
    home = Path(tempfile.mkdtemp())
    (home / "mcp.json").write_text(
        json.dumps(
            {"servers": [{"name": "k", "url": "https://h/mcp", "auth_env": "SOME_KEY"}]}
        ),
        encoding="utf-8",
    )
    _list(home)
    out = capsys.readouterr().out
    assert "$SOME_KEY" in out
    assert "not signed in" not in out


def test_a_local_server_is_not_described_as_lacking_a_credential(capsys):
    """A stdio server needs no credential at all. Listing it as unauthenticated
    would put a problem on screen that does not exist."""
    home = Path(tempfile.mkdtemp())
    (home / "mcp.json").write_text(
        json.dumps({"servers": [{"name": "fs", "command": "echo"}]}), encoding="utf-8"
    )
    _list(home)
    assert "no credential needed" in capsys.readouterr().out


def test_under_an_hour_is_minutes_not_a_rounded_down_zero():
    """A token minted fifty minutes ago rendered as "0h left", which reads as
    expired and is the opposite of the truth."""
    home = Path(tempfile.mkdtemp())
    path = _auth_file(home, "s", _token("a@b.c", exp_offset=57 * 60))
    line = _identity(path)
    assert "0h" not in line
    assert "m left" in line


def test_over_an_hour_stays_in_hours():
    home = Path(tempfile.mkdtemp())
    path = _auth_file(home, "s", _token("a@b.c", exp_offset=5 * 3600 + 120))
    assert "5h left" in _identity(path)
