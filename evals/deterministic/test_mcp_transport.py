"""DETERMINISTIC EVAL — the MCP connector's two transports, proven offline.

The connector was stdio-only until 2026-08-26: `_connect_all` built
`StdioServerParameters` unconditionally, so a remote MCP server could not be
reached at all. This file pins the branch that fixed it.

Nothing here touches the network. The stdio case spawns the demo server that
ships in `examples/`; the HTTP case spawns that same server in
`streamable-http` mode on a loopback port. Both are real MCP sessions over a
real transport — not mocks — which is the point: the two bugs this connector
has actually had (an SDK rename, and a transport that was never implemented)
would both sail past a mocked session.

What is deliberately NOT asserted: that a particular remote service accepts
our credential. That needs a live server and a real key, and belongs in a
hand-run check, not in CI.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from waku.tools.mcp_client import MCPBridge, _model_safe_name

pytest.importorskip("mcp", reason="the MCP connector is an optional extra")

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "examples" / "mcp_demo_server.py"


def _bridge(spec: dict) -> MCPBridge:
    path = Path(tempfile.mkdtemp()) / "mcp.json"
    path.write_text(json.dumps({"servers": [spec]}), encoding="utf-8")
    return MCPBridge(path, timeout=30.0)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- tool names the model provider will actually accept ---------------------


def test_dotted_tool_names_survive_the_provider_contract():
    """MCP allows a dot in a tool name; Anthropic and OpenAI do not.

    waku-memory publishes `memory.remember` and five siblings, so this is not
    a hypothetical shape — connecting to it emitted
    `waku_memory_memory.remember`, which the provider rejects before the model
    sees the turn. The bug looks like the model being broken, not the name.
    """
    assert _model_safe_name("waku_memory", "memory.remember") == "waku_memory_memory_remember"
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", _model_safe_name("srv", "a.b/c d"))


def test_long_names_are_truncated_to_the_limit_and_stay_legal():
    """64 characters is the provider's ceiling. Truncating must not leave a
    leading underscore, which reads as a private name to a model."""
    name = _model_safe_name("s" * 40, "t" * 40)
    assert len(name) <= 64
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
    assert not name.startswith("_")


def test_sanitising_does_not_change_what_is_sent_to_the_server():
    """The rename is presentation. Dispatch must still use the server's own
    name, or every renamed tool becomes an unknown-tool error at call time."""
    bridge = _bridge({"name": "demo", "command": sys.executable, "args": [str(DEMO)]})
    tools = bridge.start()
    try:
        tool = next(t for t in tools if t.name == "demo_reverse_text")
        # Round-tripping proves the far side accepted the name we sent.
        assert tool.fn(text="waku").strip() == "ukaw"
    finally:
        bridge.close()


# --- config shape: two transports, chosen by which key is present -----------


def test_naming_both_transports_is_refused_not_resolved(capsys):
    """A server entry with `url` AND `command` is a mistake, not a precedence
    question. Silently preferring one would run the transport the author did
    not mean and give no sign of it."""
    bridge = _bridge({"name": "x", "url": "http://127.0.0.1:1/mcp", "command": "echo"})
    assert bridge.start() == []
    assert "pick one transport" in capsys.readouterr().out
    bridge.close()


def test_missing_credential_names_the_variable(capsys):
    """`auth_env` names a variable that is not exported.

    The failure has to say so. Connecting anonymously instead would earn a 401
    the SDK reports as a generic error, and the user would read a missing
    export as a broken server.
    """
    os.environ.pop("WAKU_TEST_KEY_ABSENT", None)
    bridge = _bridge(
        {"name": "x", "url": "http://127.0.0.1:1/mcp", "auth_env": "WAKU_TEST_KEY_ABSENT"}
    )
    assert bridge.start() == []
    assert "WAKU_TEST_KEY_ABSENT is not set" in capsys.readouterr().out
    bridge.close()


def test_config_error_does_not_get_an_auth_hint(capsys):
    """The auth hint is for transport failures. Printing it under a config
    refusal points the reader at the wrong thing entirely."""
    bridge = _bridge({"name": "x", "url": "http://127.0.0.1:1/mcp", "command": "echo"})
    bridge.start()
    assert "holds a current credential" not in capsys.readouterr().out
    bridge.close()


# --- real sessions, both transports -----------------------------------------


def test_stdio_still_round_trips():
    """The path that already worked. Kept because the HTTP branch refactored
    the function it lives in, and because an SDK rename broke tool listing
    here once already (`inputSchema` -> `input_schema`)."""
    bridge = _bridge({"name": "demo", "command": sys.executable, "args": [str(DEMO)]})
    tools = bridge.start()
    try:
        assert "demo_reverse_text" in [t.name for t in tools]
        # A tool with no input schema is unusable by the model even when it
        # lists fine, which is exactly how the rename presented.
        schema = next(t for t in tools if t.name == "demo_reverse_text").input_schema
        assert schema.get("properties", {}).get("text")
        assert bridge.call("demo", "reverse_text", {"text": "waku"}).strip() == "ukaw"
    finally:
        bridge.close()


def test_streamable_http_round_trips_with_an_auth_header():
    """The branch this file exists for.

    The header is not verified by the demo server -- it accepts anything --
    so this proves the transport carries a session and a tool call, not that
    a credential was accepted. The credential path is proven by the server
    that does check: a live 401 says `api key is unknown or revoked` rather
    than `bearer token required`, which is only reachable if the header
    arrived.
    """
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, str(DEMO), "--http", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(60):
            if server.poll() is not None:
                pytest.fail("demo server exited before it listened")
            try:
                urllib.request.urlopen(url, timeout=0.5)
                break
            except urllib.error.HTTPError:
                break  # listening; a bare GET is a 400/406 by design
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail(f"demo server never listened on {port}")

        os.environ["WAKU_TEST_KEY_PRESENT"] = "mem_sk_not_a_real_key"
        bridge = _bridge({"name": "mem", "url": url, "auth_env": "WAKU_TEST_KEY_PRESENT"})
        try:
            assert "mem_reverse_text" in [t.name for t in bridge.start()]
            assert bridge.call("mem", "reverse_text", {"text": "harness"}).strip() == "ssenrah"
        finally:
            bridge.close()
            os.environ.pop("WAKU_TEST_KEY_PRESENT", None)
    finally:
        server.terminate()
        server.wait(timeout=10)


# --- oauth: the credential nobody has to issue ------------------------------


def test_naming_both_credentials_is_refused_not_resolved(capsys):
    """`auth_env` and `oauth` are two answers to one question.

    Same shape as the transport refusal above, and the same reason: with no
    stated precedence, picking one silently authenticates as something other
    than the author wrote.
    """
    bridge = _bridge(
        {
            "name": "x",
            "url": "http://127.0.0.1:1/mcp",
            "auth_env": "WAKU_TEST_KEY",
            "oauth": True,
        }
    )
    assert bridge.start() == []
    assert "pick one" in capsys.readouterr().out
    bridge.close()


def test_oauth_tokens_are_stored_per_server_and_not_world_readable():
    """The token file is a bearer credential: anything holding it can act as
    the user until it expires. It is written 0600, under WAKU_HOME, one file
    per server — a corrupt one should cost a single connection, not all of
    them."""
    import asyncio
    import stat

    from mcp.shared.auth import OAuthToken

    from waku.tools.mcp_oauth import FileTokenStorage

    home = Path(tempfile.mkdtemp())
    a = FileTokenStorage(home, "server_a")
    b = FileTokenStorage(home, "server_b")

    asyncio.run(a.set_tokens(OAuthToken(access_token="tok-a", token_type="Bearer")))
    asyncio.run(b.set_tokens(OAuthToken(access_token="tok-b", token_type="Bearer")))

    assert asyncio.run(a.get_tokens()).access_token == "tok-a"
    assert asyncio.run(b.get_tokens()).access_token == "tok-b"

    written = sorted(p.name for p in (home / "mcp-auth").glob("*.json"))
    assert written == ["server_a.json", "server_b.json"]

    mode = (home / "mcp-auth" / "server_a.json").stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH), "token file is readable beyond its owner"


def test_a_corrupt_token_file_costs_a_sign_in_not_a_crash():
    """Hand-edited or half-written JSON is treated as absent.

    The alternative is a harness that will not start until someone deletes a
    file whose path they have never seen.
    """
    import asyncio

    from waku.tools.mcp_oauth import FileTokenStorage

    home = Path(tempfile.mkdtemp())
    storage = FileTokenStorage(home, "server_a")
    path = home / "mcp-auth" / "server_a.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert asyncio.run(storage.get_tokens()) is None
    assert asyncio.run(storage.get_client_info()) is None


def test_a_server_name_cannot_escape_the_auth_directory():
    """Server names come from mcp.json and become filenames. A name with a
    slash or a parent reference must not write outside WAKU_HOME."""
    from waku.tools.mcp_oauth import FileTokenStorage

    home = Path(tempfile.mkdtemp())
    storage = FileTokenStorage(home, "../../etc/passwd")
    assert (home / "mcp-auth") in storage._path.parents
    assert "/" not in storage._path.name


def test_an_oauth_failure_does_not_send_you_to_auth_env():
    """An `oauth` server has no `auth_env`. Naming one in its failure sends the
    reader to a setting their config does not contain — and the connection
    error itself carries no status, so this hint is all they get."""
    from waku.tools.mcp_client import _auth_hint

    hint = _auth_hint(
        {"name": "x", "url": "https://h/mcp", "oauth": True}, Path("/home/.waku/mcp-auth")
    )
    assert "auth_env" not in hint
    assert "sign-in did not complete" in hint
    assert "/home/.waku/mcp-auth" in hint, "say where to delete, not just that one can"


def test_an_api_key_failure_names_the_variable_that_holds_the_key():
    """The other half of the same choice: with `auth_env` set, the variable it
    names is the one thing worth checking, so the hint says which."""
    from waku.tools.mcp_client import _auth_hint

    hint = _auth_hint(
        {"name": "x", "url": "https://h/mcp", "auth_env": "WAKU_MEMORY_API_KEY"}, Path("/unused")
    )
    assert "WAKU_MEMORY_API_KEY" in hint
    assert "sign-in" not in hint
