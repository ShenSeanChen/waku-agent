"""DETERMINISTIC EVAL: a person adds their own MCP server in one step (spec 025).

Until spec 025 a person who wanted a server Waku's maintainers had not written
a command for created `mcp.json` by hand. One function, `connectors.add`, now
writes the entry, and the command line, the chat tools and the dashboard card
all call it.

Nothing here opens a browser or reaches a network: a sign-in is a stub.
"""

from __future__ import annotations

import json

import pytest

from waku.tools import connectors

LINEAR = "https://mcp.linear.app/mcp"


def _servers(home):
    return json.loads((home / "mcp.json").read_text(encoding="utf-8"))["servers"]


def _write(home, servers, **more):
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"servers": servers, **more}), encoding="utf-8")


def _token(home, name):
    path = home / "mcp-auth" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": "t"}}), encoding="utf-8")
    return path


# ---- A1: add and remove ----------------------------------------------------

def test_add_a_remote_server_writes_one_entry_that_signs_in_on_its_own_page(tmp_path):
    added = connectors.add(tmp_path, "linear", url=LINEAR)

    assert _servers(tmp_path) == [{"name": "linear", "url": LINEAR, "oauth": True}]
    assert added.name == "linear" and added.spec == _servers(tmp_path)[0]
    assert added.auth == "oauth"
    assert "waku mcp login linear" in added.next


def test_add_with_a_key_names_the_environment_variable_and_never_holds_the_key(tmp_path):
    added = connectors.add(tmp_path, "github", url="https://api.githubcopilot.com/mcp/",
                           auth="GITHUB_PERSONAL_ACCESS_TOKEN")

    assert _servers(tmp_path) == [{"name": "github", "url": "https://api.githubcopilot.com/mcp/",
                                   "auth_env": "GITHUB_PERSONAL_ACCESS_TOKEN"}]
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in added.next and ".env" in added.next


def test_add_refuses_a_key_where_the_name_of_a_variable_belongs(tmp_path):
    secret = "ghp_abcDEF123456notavariablename"
    with pytest.raises(connectors.Refused) as refused:
        connectors.add(tmp_path, "github", url="https://api.githubcopilot.com/mcp/", auth=secret)

    assert secret not in str(refused.value), "a refusal never repeats what may be a key"
    assert "environment variable" in str(refused.value)
    assert not (tmp_path / "mcp.json").exists()


def test_add_a_local_command_writes_the_command_and_its_arguments(tmp_path):
    added = connectors.add(tmp_path, "fs", command="npx",
                           args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])

    assert _servers(tmp_path) == [{"name": "fs", "command": "npx", "args": [
        "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}]
    assert added.auth == "none"


def test_add_keeps_the_servers_and_the_other_keys_already_in_the_file(tmp_path):
    memory = {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True}
    _write(tmp_path, [memory], note="kept")

    connectors.add(tmp_path, "linear", url=LINEAR)

    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["note"] == "kept"
    assert [s["name"] for s in data["servers"]] == ["waku_memory", "linear"]
    assert data["servers"][0] == memory


def test_add_refuses_a_name_that_exists_and_writes_nothing(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)
    before = (tmp_path / "mcp.json").read_bytes()

    with pytest.raises(connectors.Refused, match="already"):
        connectors.add(tmp_path, "linear", url="https://other.example.com/mcp")

    assert (tmp_path / "mcp.json").read_bytes() == before


def test_add_refuses_an_address_that_is_already_there_under_another_name(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)

    with pytest.raises(connectors.Refused, match="linear"):
        connectors.add(tmp_path, "tracker", url=LINEAR + "/")

    assert len(_servers(tmp_path)) == 1


@pytest.mark.parametrize("name", ["", "has space", "../etc", "a" * 40, "semi;colon", "_lead"])
def test_add_refuses_a_name_that_is_not_a_short_plain_word(tmp_path, name):
    with pytest.raises(connectors.Refused):
        connectors.add(tmp_path, name, url=LINEAR)
    assert not (tmp_path / "mcp.json").exists()


@pytest.mark.parametrize("url", [
    "http://mcp.example.com/mcp",          # not encrypted, and not this machine
    "ftp://mcp.example.com/mcp",
    "https://user:secret@mcp.example.com/mcp",   # a credential in a config file
    "https:///mcp",
    "mcp.example.com/mcp",
    "javascript:alert(1)",
])
def test_add_refuses_an_address_that_is_not_plain_https(tmp_path, url):
    with pytest.raises(connectors.Refused):
        connectors.add(tmp_path, "srv", url=url)
    assert not (tmp_path / "mcp.json").exists()


def test_add_takes_plain_http_for_a_server_on_this_machine(tmp_path):
    connectors.add(tmp_path, "demo", url="http://127.0.0.1:8931/mcp")
    assert _servers(tmp_path)[0]["url"] == "http://127.0.0.1:8931/mcp"


def test_add_refuses_both_an_address_and_a_command_and_neither(tmp_path):
    with pytest.raises(connectors.Refused):
        connectors.add(tmp_path, "srv", url=LINEAR, command="npx")
    with pytest.raises(connectors.Refused):
        connectors.add(tmp_path, "srv")
    with pytest.raises(connectors.Refused):
        connectors.add(tmp_path, "srv", command="npx", auth="oauth")
    assert not (tmp_path / "mcp.json").exists()


def test_add_refuses_a_name_an_installed_plugin_already_brings(tmp_path, monkeypatch):
    from waku.plugins import store

    monkeypatch.setattr(store, "servers", lambda home: [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "plugin": "waku"}])

    with pytest.raises(connectors.Refused, match="plugin"):
        connectors.add(tmp_path, "waku_memory", url="https://elsewhere.example.com/mcp")


def test_add_says_so_when_the_file_is_not_valid_json_and_leaves_it(tmp_path):
    (tmp_path / "mcp.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(connectors.Refused, match="not valid JSON"):
        connectors.add(tmp_path, "linear", url=LINEAR)

    assert (tmp_path / "mcp.json").read_text(encoding="utf-8") == "{not json"


def test_remove_deletes_the_entry_and_the_token_file(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)
    connectors.add(tmp_path, "fs", command="npx", args=["x"])
    token = _token(tmp_path, "linear")
    other = _token(tmp_path, "fs")

    connectors.remove(tmp_path, "linear")

    assert [s["name"] for s in _servers(tmp_path)] == ["fs"]
    assert not token.exists()
    assert other.exists(), "another server's sign-in is not touched"


def test_remove_refuses_a_name_that_is_not_in_the_file(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)
    with pytest.raises(connectors.Refused, match="linear"):
        connectors.remove(tmp_path, "notion")
    assert len(_servers(tmp_path)) == 1


def test_remove_points_at_the_plugin_command_for_a_server_a_plugin_brings(tmp_path,
                                                                         monkeypatch):
    from waku.plugins import store

    monkeypatch.setattr(store, "servers", lambda home: [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "plugin": "waku"}])

    with pytest.raises(connectors.Refused, match="waku plugin"):
        connectors.remove(tmp_path, "waku_memory")


def test_add_treg_goes_through_the_same_function(tmp_path, monkeypatch):
    """`waku connect treg` writes its entry with `add`, so one function owns
    the file."""
    from waku.tools import treg

    seen = []
    real = connectors.add
    monkeypatch.setattr(connectors, "add",
                        lambda *a, **kw: seen.append((a, kw)) or real(*a, **kw))

    status, spec = treg.add_server(tmp_path)

    assert status == "added" and len(seen) == 1
    assert spec == {"name": "treg", "url": "https://treg.to/mcp/", "oauth": True}
    assert _servers(tmp_path) == [spec]


# ---- A2: the sign-in link, and `waku mcp add` / `waku mcp remove` -----------

SIGN_IN_LINK = "https://linear.app/oauth/authorize?client_id=waku&state=abc"


class FakeBridge:
    """Stands in for the MCP bridge of one sign-in. `start` hands the link to
    `open_url`, as the OAuth flow does, then waits for `finish` the way the
    real one waits for the browser."""

    def __init__(self, home, name, open_url, link=SIGN_IN_LINK, tools=("list_issues",)):
        import threading

        self.home, self.name, self.open_url = home, name, open_url
        self.link, self.tools = link, list(tools)
        self.finish, self.closed = threading.Event(), False

    def start(self):
        if self.link:
            self.open_url(self.link)
        if not self.finish.wait(5):
            raise TimeoutError
        _token(self.home, self.name)
        return self.tools

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _no_sign_in_left_over():
    yield
    connectors.forget_sign_ins()


def test_sign_in_returns_the_link_before_the_person_has_signed_in(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)
    made, done = [], []
    flow = connectors.SignIn(tmp_path, "linear", on_done=done.append,
                             bridge=lambda *a: made.append(FakeBridge(*a)) or made[-1])

    flow.start(wait=5)

    assert flow.link == SIGN_IN_LINK
    assert not flow.done.is_set() and done == []

    made[0].finish.set()
    assert flow.wait(5) is True
    assert flow.ok and flow.tools == ["list_issues"]
    assert done == [flow] and made[0].closed


def test_sign_in_that_fails_says_so_and_holds_no_link(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)

    class Broken(FakeBridge):
        def start(self):
            raise RuntimeError("authorization failed: access_denied")

    flow = connectors.SignIn(tmp_path, "linear", bridge=Broken).start(wait=5)

    assert flow.wait(5) is True
    assert flow.link is None and flow.ok is False and flow.tools is None


def test_sign_in_one_at_a_time_because_one_port_catches_the_redirect(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)
    connectors.add(tmp_path, "notion", url="https://mcp.notion.com/mcp")
    made = []

    def bridge(*a):
        made.append(FakeBridge(*a))
        return made[-1]

    first = connectors.start_sign_in(tmp_path, "linear", bridge=bridge, wait=5)
    again = connectors.start_sign_in(tmp_path, "linear", bridge=bridge, wait=5)
    assert again is first and len(made) == 1, "the same sign-in gives the same link"

    with pytest.raises(connectors.Refused, match="linear"):
        connectors.start_sign_in(tmp_path, "notion", bridge=bridge, wait=5)

    made[0].finish.set()
    first.wait(5)
    second = connectors.start_sign_in(tmp_path, "notion", bridge=bridge, wait=5)
    assert second is not first
    made[1].finish.set()
    second.wait(5)


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    """`waku mcp ...` in a temporary home, with a stub sign-in and no browser.
    Returns a function: arguments -> (exit code, what was printed)."""
    from waku.tools import mcp_cli

    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    opened = []
    monkeypatch.setattr(mcp_cli, "_open_browser", opened.append)

    def start(home, name, **kw):
        return connectors.SignIn(home, name, bridge=FakeBridge).start(wait=5)

    monkeypatch.setattr(connectors, "start_sign_in", start)
    # The stub's bridge finishes as soon as the command waits for it.
    real_wait = connectors.SignIn.wait

    def wait(self, timeout=None):
        if self._bridge is not None:
            self._bridge.finish.set()
        return real_wait(self, 5)

    monkeypatch.setattr(connectors.SignIn, "wait", wait)

    def run(*argv):
        code = mcp_cli.cli_main(list(argv))
        return code, capsys.readouterr().out

    run.home, run.opened = tmp_path, opened
    return run


def test_cli_add_writes_one_entry_and_prints_a_sign_in_link(cli):
    code, out = cli("add", "linear", LINEAR)

    assert code == 0
    assert _servers(cli.home) == [{"name": "linear", "url": LINEAR, "oauth": True}]
    assert SIGN_IN_LINK in out
    assert cli.opened == [SIGN_IN_LINK], "the command line opens the browser too"
    assert "linear is connected" in out


def test_cli_a_second_add_of_the_same_name_is_refused(cli):
    cli("add", "linear", LINEAR)
    before = (cli.home / "mcp.json").read_bytes()

    code, out = cli("add", "linear", LINEAR)

    assert code == 1 and "already" in out
    assert (cli.home / "mcp.json").read_bytes() == before


def test_cli_add_with_a_key_names_the_variable_and_starts_no_sign_in(cli):
    code, out = cli("add", "github", "https://api.githubcopilot.com/mcp/",
                    "--key-env", "GITHUB_PERSONAL_ACCESS_TOKEN")

    assert code == 0 and "GITHUB_PERSONAL_ACCESS_TOKEN" in out
    assert _servers(cli.home)[0]["auth_env"] == "GITHUB_PERSONAL_ACCESS_TOKEN"
    assert cli.opened == [] and SIGN_IN_LINK not in out


def test_cli_add_a_local_command_after_two_dashes(cli):
    code, _ = cli("add", "fs", "--", "npx", "-y",
                    "@modelcontextprotocol/server-filesystem", "/tmp")

    assert code == 0
    assert _servers(cli.home) == [{"name": "fs", "command": "npx", "args": [
        "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}]
    assert cli.opened == []


def test_cli_remove_deletes_the_entry_and_the_token_file(cli):
    cli("add", "linear", LINEAR)
    token = cli.home / "mcp-auth" / "linear.json"
    assert token.exists()

    code, out = cli("remove", "linear")

    assert code == 0 and "linear" in out
    assert _servers(cli.home) == [] and not token.exists()

    code, out = cli("remove", "linear")
    assert code == 1 and "No connector called 'linear'" in out


def test_cli_add_without_enough_to_go_on_prints_the_usage(cli):
    code, out = cli("add", "linear")

    assert code == 1
    assert "waku mcp add <name> <https address>" in out
    assert not (cli.home / "mcp.json").exists()


def test_cli_help_names_add_and_remove():
    import waku.__main__ as entry

    assert "waku mcp add <name> <url>" in entry.__doc__
    assert "waku mcp remove <name>" in entry.__doc__
