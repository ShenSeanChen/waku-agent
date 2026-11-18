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


def _owner_heard(home):
    from waku.db import connect

    conn = connect(home)
    try:
        return [row[0] for row in conn.execute("SELECT body FROM outbox ORDER BY id")]
    finally:
        conn.close()


def test_the_link_says_how_long_it_works(tmp_path):
    """The third live run: the link was opened thirteen hours later and the
    reply had never said that it stops working."""
    from waku.tools.mcp_oauth import SIGN_IN_TIMEOUT, sign_in_minutes

    connectors.add(tmp_path, "linear", url=LINEAR)
    flow = connectors.SignIn(tmp_path, "linear", bridge=FakeBridge).start(wait=5)

    said = connectors.link_words("linear", flow)

    assert SIGN_IN_TIMEOUT >= 900, "a link sent to a phone is opened at the computer later"
    assert f"works for {sign_in_minutes()} minutes" in said and SIGN_IN_LINK in said


def test_the_owner_hears_when_the_link_stops_working(tmp_path, monkeypatch):
    """The third live run: the wait ran out and nothing was sent, so the
    owner opened a dead link the next morning."""
    from waku.tools.mcp_oauth import sign_in_minutes

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    connectors.add(tmp_path, "linear", url=LINEAR)

    class TimesOut(FakeBridge):
        def start(self):
            self.open_url(self.link)
            raise RuntimeError("timed out waiting for the browser sign-in to come back")

    flow = connectors.SignIn(tmp_path, "linear", bridge=TimesOut,
                             on_done=lambda done: connectors.tell_owner(tmp_path, done))
    flow.start(wait=5)
    assert flow.wait(5) is True

    heard = _owner_heard(tmp_path)
    assert len(heard) == 1
    assert "no longer works" in heard[0] and f"{sign_in_minutes()} minutes" in heard[0]
    assert 'Say "connect linear"' in heard[0]
    assert "http" not in heard[0] and "timed out" not in heard[0]


def test_a_sign_in_that_never_had_a_link_sends_nothing_later(tmp_path, monkeypatch):
    """The reply that started it already said why."""
    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    connectors.add(tmp_path, "linear", url=LINEAR)

    class Broken(FakeBridge):
        def start(self):
            raise RuntimeError("could not reach the server")

    flow = connectors.SignIn(tmp_path, "linear", bridge=Broken,
                             on_done=lambda done: connectors.tell_owner(tmp_path, done))
    flow.start(wait=5)
    assert flow.wait(5) is True

    assert _owner_heard(tmp_path) == []


OLD_LINK = ("https://mcp.notion.com/authorize?response_type=code&client_id=abc"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A41765%2Fcallback&state=old")


def test_a_reply_does_not_repeat_a_link_that_nothing_waits_for(tmp_path):
    """The fourth live run: asked to connect Notion again, the model repeated
    the link of the evening before without a call."""
    reply = f"Here's the link again:\n\n{OLD_LINK}\n\nOnce you sign in, you're set."

    said = connectors.without_dead_links(tmp_path, reply)

    assert "http" not in said
    assert connectors.DEAD_LINK in said and said.endswith(connectors.DEAD_LINK_NEXT)
    assert "Once you sign in" in said, "the rest of the reply is kept"


def test_a_reply_keeps_the_link_of_the_sign_in_that_waits(tmp_path):
    connectors.add(tmp_path, "notion", url="https://mcp.notion.com/mcp")
    live = OLD_LINK.replace("state=old", "state=new")
    made = []
    connectors.start_sign_in(
        tmp_path, "notion", wait=5,
        bridge=lambda *a: made.append(FakeBridge(*a, link=live)) or made[-1])

    assert connectors.without_dead_links(tmp_path, f"Open this: {live}.") == f"Open this: {live}."
    said = connectors.without_dead_links(tmp_path, f"Old: {OLD_LINK}\nNew: {live}")
    assert OLD_LINK not in said and live in said
    made[0].finish.set()


def test_a_reply_with_an_ordinary_link_is_left_alone(tmp_path):
    reply = "The docs are at https://developers.notion.com/docs/mcp and the weather is fine."

    assert connectors.without_dead_links(tmp_path, reply) == reply


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
    assert "Linear is connected: 1 tool" in out


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
    code, out = cli("add")
    assert code == 1 and "waku mcp add <name> <https address>" in out

    code, out = cli("add", "jira")
    assert code == 1 and "waku mcp add jira <https address>" in out
    assert not (cli.home / "mcp.json").exists()


def test_cli_help_names_add_and_remove():
    import waku.__main__ as entry

    assert "waku mcp add <name> <url>" in entry.__doc__
    assert "waku mcp remove <name>" in entry.__doc__


# ---- A3: connect_tool and disconnect_tool, from chat ------------------------

EXAMPLE = "https://mcp.example.com/mcp"
OWNER = ("telegram", {"chat_id": 42})


@pytest.fixture
def chat(tmp_path, monkeypatch):
    """A home with an owner on Telegram, a state.db, and the two tools with a
    stub sign-in. Yields a namespace: home, conn, tools, bridges."""
    from types import SimpleNamespace

    from waku.db import connect
    from waku.tools.registry import ToolRegistry

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    monkeypatch.delenv("WAKU_APPROVE_ASK", raising=False)
    bridges = []

    def bridge(*a):
        bridges.append(FakeBridge(*a))
        return bridges[-1]

    tools = ToolRegistry()
    for tool in connectors.make_tools(
            tmp_path, sign_in=lambda home, name, **kw: connectors.start_sign_in(
                home, name, bridge=bridge, wait=5, **kw)):
        tools.register(tool)
    conn = connect(tmp_path)
    yield SimpleNamespace(home=tmp_path, conn=conn, tools=tools, bridges=bridges)
    conn.close()


def _ask(chat, tool, args):
    """One call under the gate, in a run in the owner's own chat. Returns
    (what the gate said, the run's id)."""
    from datetime import UTC, datetime

    from waku.alwayson import approvals, events, outbox, runs

    now = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
    count = chat.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    event_id = events.insert(chat.conn, source="telegram", external_id=f"m{count}",
                             session_id="telegram:42",
                             payload={"text": "connect it", "sender": {"id": "42"},
                                      "reply_to": {"chat_id": 42}})
    run = runs.start(chat.conn, "telegram:42", [event_id], now=now)
    gate = approvals.Gate(chat.home, chat.conn, now=lambda: now, tools=chat.tools)
    with outbox.running(chat.home, str(run.id), OWNER):
        deny, _ = gate(tool, args)
        if deny is None:
            deny = chat.tools.execute(tool, args)
    events.mark(chat.conn, [event_id], "done")
    runs.finish(chat.conn, run.id, "parked" if runs.get(chat.conn, run.id).pending_hash
                else "done", now=now)
    return deny, run.id


def _approve(chat, run_id):
    """The owner said yes: run the stored call exactly as the worker does."""
    from waku.alwayson import outbox, runs

    run = runs.get(chat.conn, run_id)
    with outbox.running(chat.home, str(run.id), OWNER,
                        approved_key=f"{run.id}:{run.pending_hash}"):
        return chat.tools.execute(run.pending_call["tool"], run.pending_call["args"])


def _requests(chat):
    return [row["body"] for row in chat.conn.execute("SELECT body FROM outbox ORDER BY id")]


def test_chat_connect_with_an_address_parks_and_writes_nothing_until_the_yes(chat):
    said, run_id = _ask(chat, "connect_tool", {"name_or_url": EXAMPLE})

    assert "The person you are talking to has been asked" in said
    assert not (chat.home / "mcp.json").exists(), "nothing is written before the yes"
    assert chat.bridges == [], "and no sign-in starts"
    (request,) = _requests(chat)
    assert EXAMPLE in request and "Approve? Reply yes or no." in request

    answer = _approve(chat, run_id)

    assert _servers(chat.home) == [{"name": "example", "url": EXAMPLE, "oauth": True}]
    assert SIGN_IN_LINK in answer
    assert "on the computer Waku runs on" in answer


def test_chat_connect_with_a_command_is_refused_and_the_owner_is_not_asked(chat):
    for command in ("npx -y @modelcontextprotocol/server-filesystem /tmp",
                    "python server.py", "/usr/local/bin/server", "linear; rm -rf ~"):
        said, _ = _ask(chat, "connect_tool", {"name_or_url": command})
        assert said.startswith("Not connected:"), command
        assert "waku mcp add" in said, "the command line is where a command is added"

    assert not (chat.home / "mcp.json").exists()
    assert _requests(chat) == [] and chat.bridges == []


def test_chat_connect_takes_no_other_argument_that_could_carry_a_command(chat):
    schema = chat.tools.get("connect_tool").input_schema
    assert not {"command", "args", "env"} & set(schema["properties"])
    said, _ = _ask(chat, "connect_tool", {"name_or_url": EXAMPLE, "command": "npx"})
    assert not (chat.home / "mcp.json").exists()
    assert "Error" in said or "Not connected" in said or "waits" in said


def test_chat_connect_asks_whatever_the_owner_allowed_in_the_environment(chat, monkeypatch):
    from waku.alwayson import approvals

    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "connect_tool, disconnect_tool")

    assert approvals.mode("connect_tool", {"name_or_url": EXAMPLE}) == "ask"
    assert approvals.mode("disconnect_tool", {"name": "example"}) == "ask"


def test_chat_connect_outside_a_run_writes_nothing_and_names_the_two_other_ways(chat):
    """The terminal chat and the dashboard chat cannot ask for a yes. A web
    page the agent read there could ask for a server as well as the person."""
    said = chat.tools.execute("connect_tool", {"name_or_url": EXAMPLE})

    assert said.startswith("Not connected:")
    assert "waku mcp add" in said and "dashboard" in said
    assert not (chat.home / "mcp.json").exists()


def test_chat_connect_again_gives_the_link_again_and_adds_nothing_twice(chat):
    _, first = _ask(chat, "connect_tool", {"name_or_url": EXAMPLE})
    _approve(chat, first)
    chat.conn.execute("UPDATE runs SET status = 'done', pending_hash = '' WHERE id = ?",
                      (first,))
    chat.conn.commit()

    _, second = _ask(chat, "connect_tool", {"name_or_url": EXAMPLE + "/"})
    answer = _approve(chat, second)

    assert len(_servers(chat.home)) == 1
    assert SIGN_IN_LINK in answer and len(chat.bridges) == 1


def test_chat_the_owner_is_told_once_when_the_sign_in_is_done(chat):
    _, run_id = _ask(chat, "connect_tool", {"name_or_url": EXAMPLE})
    _approve(chat, run_id)
    before = len(_requests(chat))

    chat.bridges[0].finish.set()
    connectors.sign_in_waiting(chat.home).wait(5)

    rows = chat.conn.execute("SELECT channel, target, body FROM outbox ORDER BY id").fetchall()
    assert len(rows) == before + 1
    assert (rows[-1]["channel"], json.loads(rows[-1]["target"])) == OWNER
    assert rows[-1]["body"].startswith("example is connected: 1 tool")


def test_chat_disconnect_parks_and_then_removes_the_entry_and_the_token(chat):
    connectors.add(chat.home, "linear", url=LINEAR)
    token = _token(chat.home, "linear")

    said, run_id = _ask(chat, "disconnect_tool", {"name": "linear"})
    assert "The person you are talking to has been asked" in said
    assert token.exists() and len(_servers(chat.home)) == 1
    assert "linear" in _requests(chat)[0]

    answer = _approve(chat, run_id)

    assert _servers(chat.home) == [] and not token.exists()
    assert "linear" in answer


def test_chat_both_tools_are_registered_and_have_a_mode(tmp_path):
    from evals.helpers import ScriptedClient, make_waku
    from waku.alwayson import approvals

    waku = make_waku(tmp_path, client=ScriptedClient([]), episodic_store="sqlite")
    try:
        names = [schema["name"] for schema in waku.tools.schemas()]
    finally:
        waku.close()
        waku.conn.close()

    assert "connect_tool" in names and "disconnect_tool" in names
    assert approvals.mode("disconnect_tool", {"name": "x"}) == "ask"


def test_chat_ready_names_the_servers_that_connect_without_a_browser(tmp_path):
    connectors.add(tmp_path, "linear", url=LINEAR)
    connectors.add(tmp_path, "github", url="https://api.githubcopilot.com/mcp/",
                   auth="GITHUB_PERSONAL_ACCESS_TOKEN")
    assert connectors.ready(tmp_path) == frozenset({"github"})

    _token(tmp_path, "linear")
    assert connectors.ready(tmp_path) == frozenset({"github", "linear"})


def test_chat_the_always_on_agent_is_built_again_when_a_connector_is_ready(tmp_path):
    """The owner signs in while the agent runs. Its next run has the tools."""
    from types import SimpleNamespace

    from waku.alwayson import events
    from waku.alwayson.worker import AgentRun
    from waku.db import connect

    built, closed = [], []

    def factory():
        conn = connect(tmp_path)
        agent = SimpleNamespace(
            conn=conn, settings=SimpleNamespace(home=tmp_path),
            session=SimpleNamespace(session_id="telegram:42", switch=lambda s: None),
            respond=lambda text, **kw: SimpleNamespace(reply="ok", receipt={}),
            close=lambda: closed.append(1))
        built.append(agent)
        return agent

    conn = connect(tmp_path)
    run = AgentRun(factory, log=lambda line: None)

    def turn(n):
        event_id = events.insert(conn, source="telegram", external_id=str(n),
                                 session_id="telegram:42",
                                 payload={"text": "hi", "reply_to": {"chat_id": 42}})
        run("telegram:42", [events.get(conn, event_id)])
        events.mark(conn, [event_id], "done")

    try:
        connectors.add(tmp_path, "linear", url=LINEAR)
        turn(1)
        turn(2)
        assert len(built) == 1, "an entry that is not signed in changes nothing"

        _token(tmp_path, "linear")
        turn(3)
        assert len(built) == 2 and closed == [1]

        turn(4)
        assert len(built) == 2
    finally:
        run.close()
        conn.close()


def test_chat_the_always_on_agent_opens_no_browser_for_a_server_not_signed_in(
        tmp_path, monkeypatch):
    """`waku serve` runs where nobody may be looking. A server that still
    needs its sign-in is left out there, and the build does not wait for it."""
    import webbrowser

    from evals.helpers import ScriptedClient, make_waku

    opened = []
    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)
    connectors.add(tmp_path, "linear", url="http://127.0.0.1:9/mcp")

    waku = make_waku(tmp_path, client=ScriptedClient([]), episodic_store="sqlite",
                     mcp_sign_in=False)
    try:
        bridge = waku.tools.mcp_bridge
        assert not bridge.connected("linear") and bridge.servers == []
    finally:
        waku.close()
        waku.conn.close()
    assert opened == []


# ---- A4: the dashboard card "Connectors" and its routes ----------------------

STATIC = __import__("pathlib").Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"


@pytest.fixture
def dash(tmp_path, monkeypatch):
    """The dashboard's three handlers in a temporary home, with a stub
    sign-in."""
    from types import SimpleNamespace

    from waku.ops import dashboard

    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    real = connectors.start_sign_in
    monkeypatch.setattr(
        connectors, "start_sign_in",
        lambda home, name, **kw: real(home, name, bridge=FakeBridge, wait=5, **kw))
    return SimpleNamespace(home=tmp_path, connectors_data=dashboard.connectors_data,
                           connectors_add=dashboard.connectors_add,
                           connectors_remove=dashboard.connectors_remove)


def test_dashboard_add_writes_the_entry_and_hands_back_the_sign_in_link(dash):
    out = dash.connectors_add({"url": EXAMPLE})

    assert out["ok"] is True and out["name"] == "example"
    assert out["link"] == SIGN_IN_LINK
    assert "on the computer Waku runs on" in out["message"]
    assert _servers(dash.home) == [{"name": "example", "url": EXAMPLE, "oauth": True}]
    json.dumps(out)


def test_dashboard_add_never_takes_a_command(dash):
    for payload in ({"url": "npx -y some-server"}, {"command": "npx", "args": ["-y", "x"]},
                    {"url": EXAMPLE, "command": "npx"}, {}):
        out = dash.connectors_add(payload)
        assert "error" in out, payload
    assert not (dash.home / "mcp.json").exists()


def test_dashboard_data_lists_each_server_with_its_account_and_whether_it_can_go(
        dash, monkeypatch):
    from waku.plugins import store

    connectors.add(dash.home, "linear", url=LINEAR)
    connectors.add(dash.home, "fs", command="npx", args=["x"])
    monkeypatch.setattr(store, "servers", lambda home: [
        {"name": "waku_memory", "url": "https://api.waku.one/mcp", "oauth": True,
         "plugin": "waku"}])

    data = dash.connectors_data()

    assert data["available"] is True
    rows = {row["name"]: row for row in data["servers"]}
    assert list(rows) == ["linear", "fs", "waku_memory"]
    assert rows["linear"]["address"] == LINEAR and rows["linear"]["account"] == "not signed in"
    assert rows["linear"]["removable"] is True and rows["fs"]["removable"] is True
    assert rows["waku_memory"]["removable"] is False, "a plugin's server goes with the plugin"
    assert rows["linear"]["tools"] is None, "no agent is running, so no count is known"
    assert data["sign_in"] is None
    json.dumps(data)


def test_dashboard_data_carries_the_link_while_a_sign_in_waits(dash):
    dash.connectors_add({"url": EXAMPLE})

    assert dash.connectors_data()["sign_in"] == {"name": "example", "link": SIGN_IN_LINK}


def test_dashboard_remove_takes_the_server_off_the_card(dash):
    connectors.add(dash.home, "linear", url=LINEAR)
    token = _token(dash.home, "linear")

    out = dash.connectors_remove({"name": "linear"})

    assert out == {"ok": True, "removed": "linear"}
    assert dash.connectors_data()["servers"] == [] and not token.exists()
    assert "error" in dash.connectors_remove({"name": "linear"})
    assert "error" in dash.connectors_remove({})


def test_dashboard_card_is_hidden_where_the_platform_sets_the_servers(dash, monkeypatch):
    monkeypatch.setenv("WAKU_PROVIDER", "waku-platform")
    assert dash.connectors_data()["available"] is False


def test_dashboard_routes_are_blocked_on_hosted_with_a_sentence(dash):
    from hosted.core import policy

    for method, path in (("GET", "/api/connectors"), ("POST", "/api/connectors/add"),
                         ("POST", "/api/connectors/remove")):
        outcome = policy.decide(method, path, {"url": EXAMPLE})
        assert (outcome.verdict, outcome.status) == ("block", 403), path
        assert outcome.message == ("Connectors are not available on hosted Waku. A hosted "
                                   "container's servers are set up when you sign in.")


def test_dashboard_card_has_an_address_field_and_a_remove_button_on_each_row():
    script = (STATIC / "js" / "connectors.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    views = (STATIC / "js" / "views.js").read_text(encoding="utf-8")

    assert "function connectorsCard(" in script and 'title: "Connectors"' in script
    assert "function addConnector(" in script and "function removeConnector(" in script
    assert '"/api/connectors/add"' in script and '"/api/connectors/remove"' in script
    assert 'type="url"' in script
    assert "connectorsCard(" in views, "Tools > MCP shows the card"
    assert index.index("/static/js/connectors.js") < index.index("/static/js/main.js")
    assert script.isascii(), "no emoji in the dashboard"
    assert script.count("esc(") >= 5, "a name, an address and a link are text from outside"


# ---- B: the starter list ----------------------------------------------------

FIELDS = {"name", "title", "url", "auth", "docs", "writes"}


def test_starter_list_every_entry_has_its_six_fields_and_an_https_address():
    """Spec 025, acceptance 3."""
    import re
    from urllib.parse import urlparse

    listed = connectors.starter()

    assert listed, "the starter list is not empty"
    for key, entry in listed.items():
        assert set(entry) == FIELDS, key
        assert entry["name"] == key and re.fullmatch(r"[a-z][a-z0-9_]{1,31}", key), key
        assert entry["title"].strip(), key
        for field in ("url", "docs"):
            parts = urlparse(entry[field])
            assert parts.scheme == "https" and parts.hostname, (key, field)
        assert entry["auth"] == "oauth" or re.fullmatch(r"[A-Z][A-Z0-9_]+", entry["auth"]), key
        assert isinstance(entry["writes"], list) and entry["writes"], key
        assert all(isinstance(p, str) and p.strip() for p in entry["writes"]), key


def test_starter_list_names_each_vendors_own_server():
    listed = connectors.starter()

    assert {key: (entry["url"], entry["auth"]) for key, entry in listed.items()} == {
        "linear": ("https://mcp.linear.app/mcp", "oauth"),
        "notion": ("https://mcp.notion.com/mcp", "oauth"),
        "github": ("https://api.githubcopilot.com/mcp/", "GITHUB_PERSONAL_ACCESS_TOKEN"),
    }


def test_starter_list_ships_inside_the_package():
    import tomllib
    from pathlib import Path

    import waku

    root = Path(waku.__file__).resolve().parent
    assert (root / "connectors.toml").is_file()
    wheel = tomllib.loads((root.parent / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["waku"], "the wheel ships every file under waku/"


def test_starter_list_a_name_alone_is_enough_to_add_it(tmp_path):
    added = connectors.add(tmp_path, "linear")
    assert added.spec == {"name": "linear", "url": LINEAR, "oauth": True}

    github = connectors.add(tmp_path, "GitHub")
    assert github.spec == {"name": "github", "url": "https://api.githubcopilot.com/mcp/",
                           "auth_env": "GITHUB_PERSONAL_ACCESS_TOKEN"}
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in github.next
    assert "https://" in github.next, "where the key comes from is one link away"


def test_starter_list_a_name_that_is_not_on_it_is_refused_with_the_names(tmp_path):
    with pytest.raises(connectors.Refused) as refused:
        connectors.add(tmp_path, "jira")

    assert "linear" in str(refused.value) and "waku mcp add jira <https address>" in str(
        refused.value)
    assert not (tmp_path / "mcp.json").exists()


def test_starter_list_cli_add_linear_writes_one_entry_and_prints_a_sign_in_link(cli):
    """Spec 025, acceptance 1."""
    code, out = cli("add", "linear")
    assert code == 0 and SIGN_IN_LINK in out
    assert _servers(cli.home) == [{"name": "linear", "url": LINEAR, "oauth": True}]

    code, out = cli("add", "linear")
    assert code == 1 and "already" in out
    assert len(_servers(cli.home)) == 1

    token = cli.home / "mcp-auth" / "linear.json"
    assert token.exists()
    code, _ = cli("remove", "linear")
    assert code == 0 and _servers(cli.home) == [] and not token.exists()


def test_starter_list_chat_connects_by_name_and_the_request_says_which_server(chat):
    said, run_id = _ask(chat, "connect_tool", {"name_or_url": "Linear"})

    assert "The person you are talking to has been asked" in said
    assert "Connect Linear (https://mcp.linear.app/mcp)" in _requests(chat)[0]

    answer = _approve(chat, run_id)

    assert _servers(chat.home) == [{"name": "linear", "url": LINEAR, "oauth": True}]
    assert SIGN_IN_LINK in answer


def test_starter_list_dashboard_lists_it_and_adds_one_by_its_name(dash):
    before = dash.connectors_data()["starter"]
    assert [(row["name"], row["title"], row["added"]) for row in before] == [
        (key, entry["title"], False) for key, entry in connectors.starter().items()]

    out = dash.connectors_add({"name": "linear"})

    assert out["ok"] is True and out["name"] == "linear" and out["link"] == SIGN_IN_LINK
    after = {row["name"]: row["added"] for row in dash.connectors_data()["starter"]}
    assert after["linear"] is True and after["notion"] is False
    assert "error" in dash.connectors_add({"name": "jira"})
    assert "error" in dash.connectors_add({"name": "linear", "url": EXAMPLE})


def test_starter_list_card_draws_one_button_for_each_entry():
    script = (STATIC / "js" / "connectors.js").read_text(encoding="utf-8")
    assert "c.starter" in script and "addConnector(" in script
    assert "{name" in script, "a button posts the entry's name"


@pytest.mark.parametrize("url, name", [
    ("https://mcp.linear.app/mcp", "linear"),
    ("https://mcp.example.com/mcp", "example"),
    ("https://api.githubcopilot.com/mcp/", "githubcopilot"),
    ("https://example.com/mcp", "example"),
    ("http://127.0.0.1:9/mcp", "local"),       # was "0" until the card was tried in a browser
    ("http://localhost:8931/mcp", "local"),
    ("https://10.0.0.5/mcp", "server"),
])
def test_chat_a_server_nobody_named_is_named_after_its_host(url, name):
    assert connectors.name_for(url) == name


def test_chat_a_server_from_chat_never_takes_a_name_waku_reserves(tmp_path):
    """Waku Memory's tools run without a question, and the server counts as
    Waku Memory by its name too. A host that spells that name gets another."""
    message = connectors.connect(
        tmp_path, "https://mcp.waku_memory.io/mcp",
        sign_in=lambda home, name, **kw: connectors.SignIn(home, name, bridge=FakeBridge))
    assert "waku_memory_2" in message
    assert [s["name"] for s in _servers(tmp_path)] == ["waku_memory_2"]


# ---- C3: the "connected" message counts the tools that ask -------------------

def _listed(server, *tools):
    """Tools as the bridge lists them: (name on the server, read-only mark)."""
    from waku.tools import mcp_client

    return [mcp_client.as_tool(server, {"name": name, "description": "", "readOnly": mark},
                               call=lambda *a: "ok") for name, mark in tools]


def test_connected_line_counts_the_writes_of_a_starter_server(tmp_path):
    connectors.add(tmp_path, "linear")
    tools = _listed("linear", ("list_issues", False), ("get_issue", False),
                    ("create_issue", False), ("save_comment", False), ("delete_comment", False))

    assert connectors.connected_line("linear", tools, tmp_path) == (
        "Linear is connected: 5 tools, 3 of them write and will ask first.")


def test_connected_line_for_a_server_off_the_list_says_why_its_tools_ask(tmp_path):
    connectors.add(tmp_path, "example", url=EXAMPLE)
    tools = _listed("example", ("search", True), ("create_thing", False), ("list", False))

    assert connectors.connected_line("example", tools, tmp_path) == (
        "example is connected: 3 tools, 2 of them will ask first. This server is not on "
        "the starter list, so a tool asks first unless the server marks it read-only, "
        "until you say otherwise.")


def test_connected_line_says_so_when_nothing_asks(tmp_path):
    from waku.alwayson import approvals
    from waku.db import connect

    connectors.add(tmp_path, "linear")
    tools = _listed("linear", ("list_issues", False), ("create_issue", False))
    conn = connect(tmp_path)
    try:
        approvals.store(conn, "linear", allow=True)
    finally:
        conn.close()

    assert connectors.connected_line("linear", tools, tmp_path) == (
        "Linear is connected: 2 tools, and none of them asks first.")
    assert connectors.connected_line("linear", tools[:1], tmp_path) == (
        "Linear is connected: 1 tool, and it does not ask first.")


def test_connected_line_reaches_the_owner_after_the_sign_in(chat):
    _, run_id = _ask(chat, "connect_tool", {"name_or_url": "linear"})
    _approve(chat, run_id)
    chat.bridges[0].tools = _listed("linear", ("list_issues", False), ("create_issue", False))
    chat.bridges[0].finish.set()
    connectors.sign_in_waiting(chat.home).wait(5)

    told = chat.conn.execute("SELECT body FROM outbox ORDER BY id DESC").fetchone()[0]
    assert told == "Linear is connected: 2 tools, 1 of them writes and will ask first."


def test_connected_line_is_printed_by_the_command_line(cli):
    _, out = cli("add", "linear")
    assert "Linear is connected: 1 tool, and it does not ask first." in out
