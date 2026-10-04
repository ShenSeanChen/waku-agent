"""DETERMINISTIC EVAL -- the chat column alone, for waku.one to frame (spec 008).

The container's half of the embedded chat:

  A. GET /embed/chat serves the dashboard's chat column and nothing else of
     the dashboard, with the allowlist the gateway sent it (or the default).
  B. The `report` event draws a card with "Open report", and a
     `consolidation` event's `kept` lists the facts, in the dashboard's chat
     too -- rendered here from recorded events, in node.
  F. The page posts to window.parent only with the framing page's origin as
     the target, only when that origin is on its allowlist, and never "*".

The gateway's half (codes, cookie, framing headers, the 403s) is in
evals/deterministic/hosted/test_embed.py. CI has no browser; the JavaScript
runs in node against a stub DOM, and skips where node is absent, like
test_static_js_parses.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from evals.helpers import ScriptedClient, make_waku
from waku.ops import browser_agent, dashboard

STATIC = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static"
EMBED = (STATIC / "embed.html").read_text(encoding="utf-8")
JS = STATIC / "js"
DEFAULT = "https://www.waku.one https://waku.one https://dev.waku.one"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

# A turn that saved a report and kept one fact, as /api/chat/stream sends it.
REPORT = {"kind": "report", "title": "Mem0 competitors, 2026-10-03",
          "memory_id": "mem-7f3a", "scope": "project:Company brain",
          "summary": ["Zep raised $12M.", "Letta ships a hosted tier.",
                      "Supermemory is open source.", "A fourth bullet is never shown."]}
KEPT = {"kind": "consolidation", "new_facts": 1,
        "kept": [{"subject": "mem0", "content": "Mem0 raised a Series A in 2026.",
                  "project": "Company brain", "memory_id": "mem-91c0"}]}
DONE = {"kind": "done", "reply": "Three findings. Report saved.", "tools": [],
        "gate": None, "iterations": 2, "latency_ms": 900, "model": "m",
        "report": {k: v for k, v in REPORT.items() if k != "kind"},
        "consolidation": {k: v for k, v in KEPT.items() if k != "kind"}}


# --- A. the page --------------------------------------------------------------


def test_the_page_is_the_chat_column_and_nothing_else():
    for needed in ('id="dock"', 'id="docklog"', 'id="dmsg"', 'id="dsend"',
                   'id="modelchip"', 'id="teletoggle"', "newChat()",
                   "toggleSessMenu(event)", "toggleTele()", "toggleModelMenu(event)"):
        assert needed in EMBED, needed
    for absent in ('id="nav"', "<main", 'id="view"', "#compare", "#judgment",
                   "#settings", "#database", 'id="mic"', 'id="dock-close"'):
        assert absent not in EMBED, f"{absent} is the dashboard's, not the chat's"


def test_the_page_loads_the_chat_scripts_and_no_view():
    scripts = re.findall(r'<script src="/static/js/([a-z]+\.js)"></script>', EMBED)
    assert scripts == ["util.js", "theme.js", "ui.js", "render.js", "dock.js", "embed.js"]
    assert not re.search(r"<script>", EMBED), "no inline script: the page runs only its files"
    for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', EMBED):
        assert (STATIC / ref[len("/static/"):]).is_file(), ref


def test_the_page_carries_the_allowlist_it_was_served_with():
    assert 'data-embed-origins="@@EMBED_ORIGINS@@"' in EMBED
    page = dashboard.embed_page(None).decode()
    assert f'data-embed-origins="{DEFAULT}"' in page
    told = dashboard.embed_page("https://dev.waku.one http://localhost:3000").decode()
    assert 'data-embed-origins="https://dev.waku.one http://localhost:3000"' in told
    forged = dashboard.embed_page('https://dev.waku.one "><script>x</script> *').decode()
    assert 'data-embed-origins="https://dev.waku.one"' in forged
    assert "<script>x" not in forged


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(browser_agent, "_dashboard_session", "s-20261003-091500")
    app = make_waku(tmp_path / "home", client=ScriptedClient([]))
    app.conn.execute("INSERT INTO chat_log (role, content, session_id, source) "
                     "VALUES ('user', 'who competes with mem0?', 's-20261003-091500', 'dashboard')")
    app.conn.commit()
    return app


@pytest.fixture
def server(home):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str, headers: dict | None = None) -> tuple[str, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.headers.get("Content-Type"), response.read()


def test_localhost_serves_the_embed_page(server):
    kind, body = _get(server + "/embed/chat")
    assert kind.startswith("text/html")
    assert f'data-embed-origins="{DEFAULT}"'.encode() in body
    _, told = _get(server + "/embed/chat", {"X-Waku-Embed-Origins": "https://dev.waku.one"})
    assert b'data-embed-origins="https://dev.waku.one"' in told


def test_the_header_state_is_the_chats_and_no_more(server):
    """The embed reads this instead of /api/data, which an embed session is
    refused: the conversations, the current one, the model and the pins."""
    _, body = _get(server + "/api/session?action=state")
    state = json.loads(body)
    assert set(state) == {"ok", "sessions", "current_session", "settings"}
    assert state["current_session"] == "s-20261003-091500"
    assert [s["id"] for s in state["sessions"]] == ["s-20261003-091500"]
    assert set(state["settings"]) == {"provider", "model", "small_model", "pinned",
                                      "disabled_providers"}


# --- B and F, run in node ----------------------------------------------------------

# A stub DOM just wide enough for the chat's scripts to load and run: nothing
# here renders, the tests read the HTML strings the renderers return.
_STUB = r"""
const vm = require("vm");
const fs = require("fs");
const posts = [];
const ctx = {console, URL, TextEncoder, TextDecoder, setTimeout, clearTimeout,
             setInterval, clearInterval, JSON, Promise, Set, Date, Math};
ctx.window = ctx;
ctx.parent = SETUP.framed ? {postMessage: (m, o) => posts.push({message: m, origin: o})} : ctx;
ctx.document = {referrer: SETUP.referrer,
  body: {dataset: {embedOrigins: SETUP.origins}, classList: {contains: () => true,
         toggle(){}}},
  getElementById: () => null, querySelectorAll: () => [],
  documentElement: {dataset: {}}};
ctx.localStorage = {getItem: () => null, setItem(){}};
ctx.location = {origin: SETUP.self || "https://agent.example"};
const listeners = [];
ctx.addEventListener = (type, fn) => listeners.push({type, fn});
ctx.MutationObserver = class { observe(){} };
const sse = evs => evs.map(e => "data: " + JSON.stringify(e) + "\n\n").join("");
ctx.fetch = async (url) => {
  if (String(url).startsWith("/api/session?action=state"))
    return {ok: SETUP.state === 200, status: SETUP.state,
            headers: {get: () => "application/json"},
            json: async () => ({sessions: [], settings: {}, current_session: null})};
  const bytes = new TextEncoder().encode(sse(SETUP.events));
  let sent = false;
  return {ok: true, status: 200, headers: {get: () => "text/event-stream"},
          body: {getReader: () => ({read: async () => sent ? {done: true}
                   : (sent = true, {value: bytes, done: false})})}};
};
vm.createContext(ctx);
for (const f of SETUP.files) vm.runInContext(fs.readFileSync(f, "utf8"), ctx, {filename: f});
"""


def _node(setup: dict, body: str) -> dict:
    program = f"const SETUP = {json.dumps(setup)};\n{_STUB}\n(async () => {{\n{body}\n}})();"
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,  # noqa: S603
                         timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _files(*names: str) -> list[str]:
    return [str(JS / name) for name in names]


CHAT_FILES = _files("util.js", "ui.js", "render.js")


@needs_node
def test_the_report_card_renders_from_a_recorded_report_event():
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, f"""
    const pending = {{role: "waku", pending: true, stream: ""}};
    for (const ev of {json.dumps([REPORT, KEPT, DONE])})
      vm.runInContext("applyStreamEvent", ctx)(pending, ev);
    const card = vm.runInContext("chatTurnCard", ctx)(pending);
    const reopened = vm.runInContext("chatTurnCard", ctx)(vm.runInContext("histItem", ctx)(
      {{role: "assistant", content: "Report saved.", meta: {{report: {json.dumps(DONE["report"])}}}}}));
    console.log(JSON.stringify({{card, reopened}}));""")
    card = got["card"]
    assert "Mem0 competitors, 2026-10-03" in card
    for bullet in REPORT["summary"][:3]:
        assert bullet in card
    assert REPORT["summary"][3] not in card, "three bullets at most"
    assert 'href="https://www.waku.one/memories/mem-7f3a"' in card and ">Open report</a>" in card
    assert "Kept in memory" in card and "Mem0 raised a Series A in 2026." in card
    assert 'href="https://www.waku.one/memories/mem-91c0"' in card
    assert "Open report" in got["reopened"], "a reopened thread draws the card from its meta"


@needs_node
def test_framed_the_report_opens_on_the_site_that_framed_it():
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, f"""
    vm.runInContext("var embedParentOrigin = () => 'https://dev.waku.one';", ctx);
    const card = vm.runInContext("reportCard", ctx)({json.dumps(DONE["report"])});
    console.log(JSON.stringify({{card}}));""")
    assert 'href="https://dev.waku.one/memories/mem-7f3a"' in got["card"]


@needs_node
def test_the_report_card_escapes_what_the_model_wrote():
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, """
    const card = vm.runInContext("reportCard", ctx)({title: '<img src=x onerror=alert(1)>',
      memory_id: '"><script>', summary: ['<b>x</b>']});
    console.log(JSON.stringify({card}));""")
    assert "<img" not in got["card"] and "<script>" not in got["card"] and "<b>x" not in got["card"]


def _embed_run(referrer: str, framed: bool, *, state: int = 200) -> list[dict]:
    """Load the embed page's scripts (dock.js and theme.js stubbed: they only
    touch the DOM), send one message, and answer what was posted."""
    setup = {"files": CHAT_FILES, "referrer": referrer, "origins": DEFAULT,
             "framed": framed, "events": [REPORT, KEPT, DONE], "state": state}
    return _node(setup, f"""
    vm.runInContext(`
      function applyTheme(){{}} function currentTheme(){{ return "system"; }}
      function syncModelChip(){{}} function applyTele(){{}}
      async function loadThreadInto(){{ return null; }}`, ctx);
    vm.runInContext(fs.readFileSync({json.dumps(str(JS / "embed.js"))}, "utf8"), ctx);
    await new Promise(r => setTimeout(r, 0));
    await vm.runInContext("sendChat", ctx)({{value: "who competes with mem0?",
      tagName: "TEXTAREA_STUB", focus(){{}}}});
    console.log(JSON.stringify(posts));""")


@needs_node
def test_a_framed_chat_tells_its_allowlisted_parent_and_no_one_else():
    posts = _embed_run("https://dev.waku.one/agent?tab=chat", framed=True)
    assert posts == [
        {"message": {"source": "waku-agent", "type": "report-saved",
                     "memory_id": "mem-7f3a", "title": "Mem0 competitors, 2026-10-03"},
         "origin": "https://dev.waku.one"},
        {"message": {"source": "waku-agent", "type": "turn-done", "credits_changed": True},
         "origin": "https://dev.waku.one"},
    ]


@needs_node
@pytest.mark.parametrize(("referrer", "framed"), [
    ("https://evil.example/", True),
    ("https://dev.waku.one.evil.example/", True),
    ("", True),
    ("https://dev.waku.one/agent", False),
], ids=["foreign-parent", "lookalike", "no-referrer", "not-framed"])
def test_nothing_is_posted_to_a_parent_off_the_allowlist(referrer, framed):
    assert _embed_run(referrer, framed) == []


@needs_node
def test_an_ended_session_is_told_once():
    posts = _embed_run("https://www.waku.one/", framed=True, state=401)
    expired = [p for p in posts if p["message"]["type"] == "session-expired"]
    assert expired == [{"message": {"source": "waku-agent", "type": "session-expired"},
                        "origin": "https://www.waku.one"}]


def test_no_script_posts_to_star():
    """Spec 008 F: postMessage never uses "*". Every call in the chat's
    scripts and in the gateway's signed-out page names its target."""
    sources = {p.name: p.read_text(encoding="utf-8") for p in JS.glob("*.js")}
    hosted = Path(__file__).resolve().parents[2] / "hosted" / "gateway" / "embed.py"
    if hosted.is_file():
        sources["embed.py"] = hosted.read_text(encoding="utf-8")
    calls = {name: re.findall(r"postMessage\(([^;]*)\)", src) for name, src in sources.items()}
    assert calls.get("embed.js"), "embed.js no longer posts at all; this guard protects nothing"
    for name, found in calls.items():
        for call in found:
            assert not re.search(r"""['"`]\*['"`]""", call), f"{name}: postMessage({call})"


# --- spec 040 M3 (waku-memory), the agent's half: "Open report" in place -------------

def _open_report(referrer: str, framed: bool) -> dict:
    """Click "Open report" on a recorded card: what was posted, and whether
    the link was left to open its tab (the handler's return value)."""
    setup = {"files": CHAT_FILES, "referrer": referrer, "origins": DEFAULT,
             "framed": framed, "events": [], "state": 200}
    return _node(setup, f"""
    vm.runInContext(`
      function applyTheme(){{}} function currentTheme(){{ return "system"; }}
      function syncModelChip(){{}} function applyTele(){{}}
      async function loadThreadInto(){{ return null; }}`, ctx);
    vm.runInContext(fs.readFileSync({json.dumps(str(JS / "embed.js"))}, "utf8"), ctx);
    await new Promise(r => setTimeout(r, 0));
    const card = vm.runInContext("reportCard", ctx)({json.dumps(DONE["report"])});
    const link = {{dataset: {{memoryId: "mem-7f3a", title: "Mem0 competitors, 2026-10-03"}}}};
    const openTab = vm.runInContext("openReport", ctx)(link);
    console.log(JSON.stringify({{card, openTab, posts}}));""")


@needs_node
def test_framed_open_report_asks_the_allowlisted_parent_instead_of_a_tab():
    got = _open_report("https://dev.waku.one/agent", framed=True)
    assert got["openTab"] is False
    assert got["posts"] == [{"message": {"source": "waku-agent", "type": "open-report",
                                         "memory_id": "mem-7f3a",
                                         "title": "Mem0 competitors, 2026-10-03"},
                             "origin": "https://dev.waku.one"}]
    assert 'onclick="return openReport(this)"' in got["card"]
    assert 'data-memory-id="mem-7f3a"' in got["card"]


@needs_node
@pytest.mark.parametrize(("referrer", "framed"), [
    ("https://evil.example/", True),
    ("", True),
    ("https://dev.waku.one/agent", False),
], ids=["foreign-parent", "no-referrer", "not-framed"])
def test_unframed_or_off_the_allowlist_open_report_opens_a_tab(referrer, framed):
    got = _open_report(referrer, framed)
    assert got["openTab"] is True and got["posts"] == []


@needs_node
def test_the_dashboard_without_embed_js_opens_a_tab():
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, """
    const openTab = vm.runInContext("openReport", ctx)({dataset: {memoryId: "m"}});
    console.log(JSON.stringify({openTab, posts}));""")
    assert got == {"openTab": True, "posts": []}


# --- spec 009 C: what a tool call cost, on its card and in the turn's footer ----------

TREG_TOOL = {"kind": "tool", "tool": "treg_catalog_call_read",
             "args": {"endpoint_id": "tomba.email.find", "params": {"domain": "mem0.ai"}},
             "output": json.dumps({"status": 200, "endpoint_id": "tomba.email.find",
                                   "call_id": "call_1", "cost_usd": 0.0089, "body": {}})}
NAMED_TOOL = {"kind": "tool", "tool": "treg_catalog_call_read", "args": {},
              "output": json.dumps({"endpoint_id": "x.y", "provider": "PredictLeads",
                                    "cost_usd": 0.6})}
FREE_TOOL = {"kind": "tool", "tool": "list_events", "args": {}, "output": "No events today."}


@needs_node
def test_a_tool_card_shows_its_cost_and_provider_and_the_footer_the_total():
    done = {**DONE, "tools": [{k: v for k, v in t.items() if k != "kind"}
                              for t in (TREG_TOOL, NAMED_TOOL, FREE_TOOL)]}
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, f"""
    const pending = {{role: "waku", pending: true, stream: ""}};
    vm.runInContext("applyStreamEvent", ctx)(pending, {json.dumps(TREG_TOOL)});
    const live = vm.runInContext("streamingCard", ctx)(pending);
    vm.runInContext("applyStreamEvent", ctx)(pending, {json.dumps(done)});
    const card = vm.runInContext("chatTurnCard", ctx)(pending);
    console.log(JSON.stringify({{live, card}}));""")
    assert "$0.0089 · tomba" in got["live"], "the card shows the cost while the turn runs"
    assert "$0.0089 · tomba" in got["card"]
    assert "$0.60 · PredictLeads" in got["card"], "a named provider wins over the endpoint"
    footer = got["card"][got["card"].rindex('<div class="meta tele">'):]
    assert "0.9s" in footer and " · m · " in footer and "tools $0.61" in footer


@needs_node
def test_a_turn_with_no_priced_tool_shows_no_cost():
    done = {**DONE, "tools": [{k: v for k, v in FREE_TOOL.items() if k != "kind"}]}
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, f"""
    const pending = {{role: "waku", pending: true, stream: ""}};
    vm.runInContext("applyStreamEvent", ctx)(pending, {json.dumps(done)});
    console.log(JSON.stringify({{card: vm.runInContext("chatTurnCard", ctx)(pending)}}));""")
    assert "tool-cost" not in got["card"] and "tools $" not in got["card"]


@needs_node
def test_the_used_list_renders_what_the_brain_already_knew():
    used = [{"id": "rep-0915", "text": "Zep and Letta sell hosted agent memory.",
             "created_at": "2026-09-15", "kind": "semantic", "report": True,
             "title": "Mem0 competitors, 2026-09-15"}]
    done = {**DONE, "used": used}
    got = _node({"files": CHAT_FILES, "referrer": "", "origins": DEFAULT, "framed": False,
                 "events": [], "state": 200}, f"""
    const pending = {{role: "waku", pending: true, stream: ""}};
    vm.runInContext("applyStreamEvent", ctx)(pending, {json.dumps(done)});
    const card = vm.runInContext("chatTurnCard", ctx)(pending);
    const reopened = vm.runInContext("chatTurnCard", ctx)(vm.runInContext("histItem", ctx)(
      {{role: "assistant", content: "ok", meta: {{used: {json.dumps(used)}}}}}));
    console.log(JSON.stringify({{card, reopened}}));""")
    assert "Used from memory" in got["card"] and "2026-09-15" in got["card"]
    assert 'href="https://www.waku.one/memories/rep-0915"' in got["card"]
    assert "Used from memory" in got["reopened"]


# --- spec 040 P2 (waku-memory), the frame's half: waku.one asks for a new chat -------

NEW_CHAT = {"source": "waku-console", "type": "new-chat"}


def _new_chat(message: dict, *, origin: str = "https://dev.waku.one", from_parent: bool = True,
              chat: int = 2, self_origin: str = "https://agent.example") -> dict:
    """Load embed.js framed by dev.waku.one, put `chat` messages in the column,
    dispatch one window "message" event to the listener embed.js added, and
    answer how many times newChat() ran and whether the event was accepted."""
    setup = {"files": CHAT_FILES, "referrer": "https://dev.waku.one/agent",
             "origins": DEFAULT, "framed": True, "events": [], "state": 200,
             "self": self_origin}
    return _node(setup, f"""
    vm.runInContext(`
      function applyTheme(){{}} function currentTheme(){{ return "system"; }}
      function syncModelChip(){{}} function applyTele(){{}}
      async function loadThreadInto(){{ return null; }}
      var newChats = 0; function newChat(){{ newChats += 1; CHAT.length = 0; }}`, ctx);
    vm.runInContext(fs.readFileSync({json.dumps(str(JS / "embed.js"))}, "utf8"), ctx);
    await new Promise(r => setTimeout(r, 0));
    for (let i = 0; i < {chat}; i++) vm.runInContext("CHAT", ctx).push({{role: "user", text: "hi"}});
    const handlers = listeners.filter(l => l.type === "message");
    const event = {{data: {json.dumps(message)}, origin: {json.dumps(origin)},
                    source: {"ctx.parent" if from_parent else "{}"}}};
    const accepted = handlers.map(l => l.fn(event));
    console.log(JSON.stringify({{handlers: handlers.length, accepted,
      newChats: vm.runInContext("newChats", ctx)}}));""")


@needs_node
def test_an_allowlisted_parent_starts_a_new_chat():
    got = _new_chat(NEW_CHAT)
    assert got == {"handlers": 1, "accepted": [True], "newChats": 1}


def test_new_chat_is_the_buttons_function():
    """The message runs the same newChat() the "+ New chat" button runs."""
    assert 'onclick="newChat()"' in EMBED
    assert "newChat();" in (JS / "embed.js").read_text(encoding="utf-8")


@needs_node
@pytest.mark.parametrize("kwargs", [
    {"origin": "https://evil.example"},
    {"origin": "https://dev.waku.one.evil.example"},
    {"origin": "null"},
    {"origin": "https://agent.example"},
    {"origin": "https://dev.waku.one", "self_origin": "https://dev.waku.one"},
    {"from_parent": False},
    {"message": {"source": "waku-agent", "type": "new-chat"}},
    {"message": {"source": "waku-console", "type": "open-report"}},
    {"message": "new-chat"},
    {"chat": 0},
], ids=["foreign-origin", "lookalike", "opaque-origin", "own-origin",
        "own-origin-on-the-list", "not-the-parent", "wrong-source-tag", "other-type",
        "not-an-object", "already-empty"])
def test_every_other_message_is_ignored(kwargs):
    message = kwargs.pop("message", NEW_CHAT)
    got = _new_chat(message, **kwargs)
    assert got == {"handlers": 1, "accepted": [False], "newChats": 0}


# --- newChat() empties the column before the server answers ----------------------
#
# newChat() is the one function behind "+ New chat" and the new-chat message. It
# used to clear the column only after /api/session answered, so the old
# conversation stayed on screen for the gateway round trip (0.3-0.5 s on
# dev.waku.one). These run the real dock.js, hold that answer back, and look at
# the column while it is out.

def _held_new_chat(answer: str, body: str) -> dict:
    """Load the chat's scripts and dock.js (main.js's `paused` stubbed), with
    two old rows on screen in session "s-old" and /api/session held until
    `release()` answers `answer` ("ok", "refused" or "unreachable"). Then run
    `body`. `calls` lists each
    request in the order it was sent, and `log` is the painted column."""
    setup = {"files": CHAT_FILES + _files("dock.js"), "referrer": "", "origins": DEFAULT,
             "framed": False, "events": [DONE], "state": 200}
    return _node(setup, f"""
    const log = {{innerHTML: "", scrollHeight: 0}};
    ctx.document.querySelectorAll = sel => sel === ".chatlog" ? [log] : [];
    const calls = [];
    let release;
    const held = new Promise(r => {{ release = r; }});
    const streamFetch = ctx.fetch;
    ctx.fetch = async (url, opts) => {{
      calls.push(String(url));
      if (String(url) !== "/api/session") return streamFetch(url, opts);
      await held;
      if ({json.dumps(answer)} === "unreachable") throw new TypeError("Failed to fetch");
      const json = {json.dumps(answer)} === "ok" ? {{ok: true, session_id: "s-new", history: []}}
                                               : {{error: "agent is busy"}};
      return {{ok: true, status: 200, json: async () => json}};
    }};
    vm.runInContext(`var paused = false; SESSION = "s-old";
      CHAT.push({{role: "user", text: "old question"}}, {{role: "waku", reply: "old answer"}});`, ctx);
    const state = () => ({{session: vm.runInContext("SESSION", ctx),
      rows: vm.runInContext("CHAT", ctx).map(m => m.text || m.reply || ""),
      painted: log.innerHTML}});
    {body}""")


@needs_node
def test_new_chat_empties_the_column_before_the_server_answers():
    got = _held_new_chat("ok", """
    const done = vm.runInContext("newChat", ctx)();
    const during = state();
    release(); await done;
    console.log(JSON.stringify({during, after: state(), calls}));""")
    assert got["during"]["rows"] == []
    assert "old question" not in got["during"]["painted"]
    assert got["during"]["session"] == "s-old"
    assert got["after"]["session"] == "s-new" and got["after"]["rows"] == []
    assert got["calls"] == ["/api/session"]


@needs_node
@pytest.mark.parametrize(("answer", "why"), [("refused", "agent is busy"),
                                             ("unreachable", "Failed to fetch")])
def test_a_new_chat_the_server_refuses_brings_the_old_one_back(answer, why):
    got = _held_new_chat(answer, """
    const done = vm.runInContext("newChat", ctx)();
    release(); await done;
    console.log(JSON.stringify(state()));""")
    assert got["session"] == "s-old"
    assert got["rows"][:2] == ["old question", "old answer"]
    assert len(got["rows"]) == 3 and got["rows"][2].startswith("Error: could not start a new chat")
    assert why in got["rows"][2] and "old question" in got["painted"]


@needs_node
def test_a_message_typed_while_the_new_chat_opens_waits_for_it():
    """The server sends a message to its active conversation, so a message
    posted before the new one exists would land in the old one."""
    got = _held_new_chat("ok", """
    const done = vm.runInContext("newChat", ctx)();
    const sent = vm.runInContext("sendChat", ctx)({value: "first message", focus(){}});
    await new Promise(r => setTimeout(r, 10));
    const before = calls.slice();
    release(); await done; await sent;
    console.log(JSON.stringify({before, calls, after: state()}));""")
    assert got["before"] == ["/api/session"]
    assert got["calls"] == ["/api/session", "/api/chat/stream"]
    assert got["after"]["session"] == "s-new"
    assert got["after"]["rows"][0] == "first message"


@needs_node
def test_a_message_typed_while_a_refused_new_chat_opens_follows_the_old_one():
    got = _held_new_chat("refused", """
    const done = vm.runInContext("newChat", ctx)();
    const sent = vm.runInContext("sendChat", ctx)({value: "first message", focus(){}});
    release(); await done; await sent;
    console.log(JSON.stringify(state()));""")
    assert got["rows"][:4] == ["old question", "old answer",
                               "Error: could not start a new chat. agent is busy", "first message"]
