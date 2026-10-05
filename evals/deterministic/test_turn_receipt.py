"""DETERMINISTIC EVAL -- the turn receipt under every answer (spec 011).

Sean asks "research mem0's competitors". The turn makes three model calls on
claude-sonnet-5 and two small-model calls (the gate and consolidation), reads
the company brain with two Waku Memory searches that find 12 memories, calls
treg twice (Tomba $0.010, a SERP endpoint $0.020), saves a report and keeps
2 facts. Under the reply, one line reads

    claude-sonnet-5 · 12.4k in / 1.9k out · $0.064 est | treg 2 · $0.030 |
    memory 4 used · 2 kept · report saved | $0.094

The numbers in this file are spec 011's acceptance list:

  1, 2  waku/ops/receipt.py builds that receipt, field for field, and its
        keys are a closed set at every level.
  3     tool arguments and output never reach the receipt, the `done`
        payload's receipt or the chat log's meta.receipt.
  4     every side call (gate, consolidation, report, triage, quick reply)
        writes one usage.jsonl row with its kind and the turn's turn_id.
  5, 6  bad input leaves the receipt valid; a search's retrieval_trace_id
        reaches searches[].trace_id.
  7     meta.receipt equals done.receipt, and a reopened thread draws it.
  9, 10 the agent's half of the exact hosted charge: X-Waku-Turn on every
        waku-platform call, the proxy's answer used when it has one, and the
        estimate kept when it answers 404.

Eval 8 is test_static_js_parses.py and test_design_system.py. The proxy's
half of 9 and 10 is in evals/deterministic/hosted/. The rendering cases run
in node against a stub DOM and skip where node is absent, like
test_embed_chat.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.helpers import make_waku, text_block, tool_block
from waku.loop.models import TurnTagged
from waku.ops import dashboard, receipt
from waku.tools.registry import Tool

SONNET, HAIKU = "claude-sonnet-5", "claude-haiku-4-5-20251001"
JS = Path(__file__).resolve().parents[2] / "waku" / "ops" / "static" / "js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _search(n: int, **extra) -> dict:
    return {"tool": "waku_memory_memory_search", "args": {"query": "mem0"}, "read_first": True,
            "output": json.dumps({"entries": [{"id": f"m{i}"} for i in range(n)], **extra})}


def _treg(endpoint: str, usd: float) -> dict:
    return {"tool": "treg_catalog_call_read",
            "args": {"endpoint_id": endpoint, "params": {"domain": "mem0.ai"}},
            "output": json.dumps({"status": 200, "endpoint_id": endpoint, "cost_usd": usd,
                                  "body": {}})}


# The turn above, as the events it emits, in order.
EVENTS = [
    ("llm", {"kind": "gate", "model": HAIKU, "usage": {"in": 300, "out": 30}}),
    ("tool", _search(8)),
    ("tool", _search(4)),
    ("llm", {"iteration": 1, "usage": {"in": 3000, "out": 400}}),
    ("tool", _treg("tomba.email.find", 0.010)),
    ("llm", {"iteration": 2, "usage": {"in": 4000, "out": 500}}),
    ("tool", _treg("serp.google.search", 0.020)),
    ("tool", {"tool": "search_web", "args": {"q": "mem0"}, "output": "Three results. The first"}),
    ("llm", {"iteration": 3, "usage": {"in": 5000, "out": 950}}),
    ("report", {"title": "Mem0 competitors", "memory_id": "rep-1003", "scope": "global",
                "summary": ["Zep raised $12M."]}),
    ("llm", {"kind": "consolidation", "model": HAIKU, "usage": {"in": 110, "out": 22}}),
    ("consolidation", {"new_facts": 2, "kept": [
        {"subject": "Sean", "content": "Sean films on Fridays.", "project": None,
         "memory_id": "mem-a", "sent": True},
        {"subject": "Sean", "content": "Sean prices below Mem0.", "project": None,
         "memory_id": "mem-b", "sent": True}]}),
]
# 12,000 in and 1,850 out on Sonnet at $3/$15, 410 in and 52 out on Haiku at $1/$5
MODEL_USD = 0.06442
EXPECTED = {
    "turn_id": "t_7f3a",
    "model": {"id": SONNET, "in": 12410, "out": 1902, "usd": MODEL_USD, "estimate": True,
              "calls": [{"kind": "gate", "n": 1}, {"kind": "loop", "n": 3},
                        {"kind": "consolidation", "n": 1}]},
    "tools": [{"tool": "treg_catalog_call_read", "provider": "tomba", "usd": 0.01, "status": "ok"},
              {"tool": "treg_catalog_call_read", "provider": "serp", "usd": 0.02, "status": "ok"},
              {"tool": "search_web", "provider": "", "usd": None, "status": "ok"}],
    "memory": {"searches": [{"tool": "memory_search", "found": 8, "trace_id": None},
                            {"tool": "memory_search", "found": 4, "trace_id": None}],
               "used": 4, "kept": [{"memory_id": "mem-a", "sent": True},
                                     {"memory_id": "mem-b", "sent": True}],
               "report": "rep-1003"},
    "total_usd": 0.09442,
    "credits": None,
}


def _build(events=EVENTS, **kwargs) -> dict:
    args = {"turn_id": "t_7f3a", "model": SONNET, "provider": "anthropic",
            "default_model": SONNET, "used": 4, **kwargs}
    return receipt.build(events, **args)


# --- 1, 2: the example, field for field, and the closed set of keys ---------------


def test_the_example_turn_builds_exactly_its_receipt():
    assert _build() == EXPECTED


def test_the_receipt_keys_are_a_closed_set_at_every_level():
    r = _build()
    assert tuple(r) == receipt.KEYS
    assert tuple(r["model"]) == receipt.MODEL_KEYS
    assert all(set(c) == {"kind", "n"} for c in r["model"]["calls"])
    assert all(tuple(t) == receipt.TOOL_KEYS for t in r["tools"])
    assert tuple(r["memory"]) == receipt.MEMORY_KEYS
    assert all(tuple(s) == receipt.SEARCH_KEYS for s in r["memory"]["searches"])
    assert all(tuple(k) == receipt.KEPT_KEYS for k in r["memory"]["kept"])


def test_the_proxys_answer_replaces_the_estimate_and_adds_credits():
    """Spec 011 B1: on the hosted free tier the proxy knows what the turn's
    model calls were charged and the credits Waku Memory took."""
    r = _build(charges={"model_usd": 0.0712, "calls": 5, "credits": 2400})
    assert r["model"]["usd"] == 0.0712 and r["model"]["estimate"] is False
    assert r["total_usd"] == pytest.approx(0.1012) and r["credits"] == 2400
    # an answer with no number keeps the estimate and shows no credits
    r = _build(charges={"model_usd": None, "credits": None})
    assert r["model"]["estimate"] is True and r["credits"] is None


# --- 3: privacy ---------------------------------------------------------------------

# Shaped like real keys, which are far longer than any provider name.
SECRETS = ("Bearer sk-ant-api03-AUTHAUTHAUTHAUTHAUTHAUTHAUTHAUTH",
           "sk-ant-api03-APIKEYAPIKEYAPIKEYAPIKEYAPIKEY",
           "sk-ant-api03-ECHOECHOECHOECHOECHOECHOECHOECHO")


def _leaky_tool() -> dict:
    """A treg call whose arguments carry a header map and a key, and whose
    output echoes a key a person pasted."""
    return {"tool": "treg_catalog_call_read",
            "args": {"endpoint_id": "tomba.email.find", "api_key": SECRETS[1],
                     "headers": {"Authorization": SECRETS[0],
                                 "X-Treg-Route-Max-Cost": "0.05"}},
            "output": json.dumps({"endpoint_id": "tomba.email.find", "cost_usd": 0.01,
                                  "body": {"echo": SECRETS[2]}, "provider": SECRETS[2]})}


def test_no_argument_or_output_reaches_the_receipt():
    r = _build([("tool", _leaky_tool())])
    text = json.dumps(r)
    assert not any(s in text for s in SECRETS)
    assert r["tools"] == [{"tool": "treg_catalog_call_read", "provider": "tomba", "usd": 0.01,
                           "status": "ok"}]


# --- 5, 6: bad input, and the search's trace id --------------------------------------


def test_bad_input_leaves_the_receipt_valid():
    events = [
        ("tool", {"tool": "treg_catalog_call_read", "args": {},
                  "output": json.dumps({"cost_usd": -1})}),
        ("tool", {"tool": "treg_catalog_call_read", "args": {}, "output": json.dumps({})}),
        ("tool", {"tool": "treg_catalog_call_read", "args": {},
                  "output": json.dumps({"cost_usd": "a lot"})}),
        ("tool", {"tool": "waku_memory_memory_search", "args": {}, "output": "Error: timed out"}),
        ("tool", {"tool": "x", "args": None, "output": None}),
        ("llm", {"usage": None}),
        ("consolidation", {"kept": [None, {"memory_id": 7, "sent": "yes"}]}),
        ("report", {"memory_id": None}),
    ]
    r = _build(events)
    assert [t["usd"] for t in r["tools"]] == [None, None, None, None]
    assert r["memory"]["searches"] == [{"tool": "memory_search", "found": None, "trace_id": None}]
    assert r["memory"]["kept"] == [{"memory_id": None, "sent": None}]
    assert r["memory"]["report"] is None
    assert r["total_usd"] == 0 and json.dumps(r)


def test_a_search_that_names_its_trace_carries_it():
    r = _build([("tool", _search(3, retrieval_trace_id="trc-41")), ("tool", _search(1))])
    assert [s["trace_id"] for s in r["memory"]["searches"]] == ["trc-41", None]


# --- 3, 4, 7: one real turn through /api/chat/stream ---------------------------------


class Client:
    """The scripted model: plays back responses, raises an Exception that is
    in the script, and records the headers each call carried."""

    def __init__(self, script):
        self._script = list(script)
        self.headers: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.headers.append(kwargs.get("extra_headers") or {})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(blocks, tokens_in: int, tokens_out: int, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=blocks,
                           usage=SimpleNamespace(input_tokens=tokens_in,
                                                 output_tokens=tokens_out))


REPORT = """<!-- waku-report v1 -->
# Mem0 competitors, 2026-10-03

## Summary
- Zep and Letta sell hosted agent memory.
"""
FACTS = json.dumps({"facts": [{"subject": "Sean", "content": "Sean films on Fridays."}],
                    "episode": "", "company_research": False})


def _script():
    return [
        _response([text_block('{"route": "full", "reason": "research"}')], 50, 5),       # triage
        _response([text_block('{"retrieve": false, "query": "", "reason": "x"}')], 60, 6),  # gate
        _response([tool_block("treg_catalog_call_read", _leaky_tool()["args"])], 1000, 100,
                  "tool_use"),                                                         # loop 1
        _response([text_block(f"Two vendors.\n\n{REPORT}")], 2000, 300),                 # loop 2
        _response([text_block('{"company_research": false}')], 70, 7),                 # report
        _response([text_block(FACTS)], 80, 8),                                          # consolidation
    ]


def _app(tmp_path, client):
    app = make_waku(tmp_path / "home", client=client, graph_workflows=True, consolidate_every=1,
                    model=SONNET, small_model=HAIKU)
    app.tools.register(Tool(
        name="treg_catalog_call_read", description="call a treg endpoint",
        input_schema={"type": "object"},
        fn=lambda **args: _leaky_tool()["output"]))
    app.memory.remember = lambda body, scope, **kw: "mem-" + str(abs(hash(body)) % 1000)
    return app


def _stream(app, monkeypatch, message="research mem0's competitors") -> dict:
    monkeypatch.setattr(dashboard, "get_agent", lambda: app)
    monkeypatch.setattr(dashboard, "maybe_rotate_session", lambda agent: None)
    emitted = []
    dashboard.chat_stream(message, lambda kind, ev: emitted.append((kind, ev)))
    return emitted[-1][1]


def _stored_meta(app) -> dict:
    row = app.conn.execute(
        "SELECT meta FROM chat_log WHERE role = 'assistant' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["meta"])


def _ledger(app) -> list[dict]:
    path = app.settings.home / "usage.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_turn_counts_every_model_call_and_keeps_its_receipt(tmp_path, monkeypatch):
    app = _app(tmp_path, Client(_script()))
    done = _stream(app, monkeypatch)
    r = done["receipt"]

    # 4: one ledger row per call, each with its kind and this turn's id
    rows = _ledger(app)
    assert [row["kind"] for row in rows] == ["triage", "gate", "loop", "loop", "report",
                                             "consolidation"]
    assert {row["turn_id"] for row in rows} == {r["turn_id"]} and r["turn_id"].startswith("t_")
    assert [row["model"] for row in rows] == [HAIKU, HAIKU, SONNET, SONNET, HAIKU, HAIKU]
    assert r["model"]["in"] == 3260 and r["model"]["out"] == 426
    assert r["model"]["calls"] == [{"kind": "triage", "n": 1}, {"kind": "gate", "n": 1},
                                   {"kind": "loop", "n": 2}, {"kind": "report", "n": 1},
                                   {"kind": "consolidation", "n": 1}]
    assert r["tools"] == [{"tool": "treg_catalog_call_read", "provider": "tomba",
                           "usd": 0.01, "status": "ok"}]
    assert r["memory"]["report"] and len(r["memory"]["kept"]) == 1

    # 7: the chat log keeps the same receipt, and the turn id
    meta = _stored_meta(app)
    assert meta["receipt"] == r and meta["turn_id"] == r["turn_id"]

    # 3: no argument or output reached any of the three
    for text in (json.dumps(r), json.dumps(meta["receipt"])):
        assert not any(s in text for s in SECRETS)

    # A1: the trace carries the turn id on turn_start, turn_end and one receipt event
    trace = [json.loads(line) for line in next(
        (app.settings.home / "traces").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    marks = [e for e in trace if e["type"] in ("turn_start", "turn_end", "receipt")]
    assert [e["type"] for e in marks] == ["turn_start", "receipt", "turn_end"]
    assert {e["turn_id"] for e in marks} == {r["turn_id"]}
    llm = [e for e in trace if e["type"] == "llm"]
    assert [e.get("kind", "loop") for e in llm] == [row["kind"] for row in rows]


def test_the_quick_reply_is_counted(tmp_path, monkeypatch):
    script = [_response([text_block('{"route": "quick", "reason": "thanks"}')], 40, 4),
              _response([text_block("You're welcome!")], 30, 3)]
    app = _app(tmp_path, Client(script))
    app.settings.consolidate_every = 50
    done = _stream(app, monkeypatch, "thanks!")
    assert [row["kind"] for row in _ledger(app)] == ["triage", "quick"]
    assert done["receipt"]["model"]["id"] == HAIKU, "a quick turn was answered by the small model"


def test_a_failed_side_call_never_fails_the_turn(tmp_path, monkeypatch):
    """The gate raises: it fails open, records nothing, and the receipt holds."""
    script = _script()
    script[1] = RuntimeError("gate is down")
    app = _app(tmp_path, Client(script))
    done = _stream(app, monkeypatch)
    assert done["reply"].startswith("Two vendors.")
    assert "gate" not in [row["kind"] for row in _ledger(app)]
    assert tuple(done["receipt"]) == receipt.KEYS


def test_a_receipt_that_cannot_be_built_never_fails_the_turn(tmp_path, monkeypatch):
    import waku.app

    def broken(*args, **kwargs):
        raise ValueError("a bug in the receipt")

    monkeypatch.setattr(waku.app.receipts, "build", broken)
    app = _app(tmp_path, Client(_script()))
    done = _stream(app, monkeypatch)
    assert done["reply"].startswith("Two vendors.") and done["receipt"] is None
    assert "receipt" not in _stored_meta(app)


# --- 9, 10: the agent's half of the exact hosted charge ------------------------------


def test_every_platform_call_carries_the_turn_and_the_answer_is_used(tmp_path, monkeypatch):
    inner = Client(_script())
    client = TurnTagged(inner)
    asked = []
    client.charges = lambda turn_id: asked.append(turn_id) or {
        "model_usd": 0.0123, "calls": 6, "credits": 308}
    app = _app(tmp_path, client)
    r = _stream(app, monkeypatch)["receipt"]
    assert inner.headers and all(h == {"X-Waku-Turn": r["turn_id"]} for h in inner.headers)
    assert asked == [r["turn_id"]]
    assert r["model"]["usd"] == 0.0123 and r["model"]["estimate"] is False
    assert r["credits"] == 308 and r["total_usd"] == pytest.approx(0.0223)


class _Proxy(BaseHTTPRequestHandler):
    """The charges route as the proxy answers it: a known turn, or 404."""

    def do_GET(self):  # noqa: N802 -- the http.server name
        known = self.path == "/v1/turns/t_known/charges"
        body = json.dumps({"model_usd": 0.05, "calls": 3, "credits": 1250} if known else
                          {"type": "error", "error": {"type": "not_found_error",
                                                      "message": "No such turn."}}).encode()
        self.send_response(200 if known else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).seen.append(self.headers.get("x-api-key"))

    def log_message(self, *args):
        pass


def test_charges_reads_the_proxy_and_a_404_keeps_the_estimate():
    import anthropic

    _Proxy.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = TurnTagged(anthropic.Anthropic(
            api_key="platform-token", base_url=f"http://127.0.0.1:{server.server_port}"))
        assert client.charges("t_known") == {"model_usd": 0.05, "calls": 3, "credits": 1250}
        assert client.charges("t_never_seen") is None
        assert _Proxy.seen == ["platform-token", "platform-token"]
    finally:
        server.shutdown()
    r = _build(charges=None)
    assert r["model"]["estimate"] is True and r["credits"] is None


def test_an_unreachable_proxy_keeps_the_estimate():
    import anthropic

    client = TurnTagged(anthropic.Anthropic(api_key="t", base_url="http://127.0.0.1:9"))
    assert client.charges("t_any") is None


# --- 7, A4: the line in the chat, live and reopened -----------------------------------

_STUB = r"""
const vm = require("vm"), fs = require("fs");
const ctx = {console, JSON, Math, Number, String, Date, encodeURIComponent};
ctx.window = ctx;
ctx.document = {body: {classList: {contains: () => false}}, getElementById: () => null,
                querySelectorAll: () => [], createElement: () => ({content: {querySelectorAll: () => []}})};
ctx.localStorage = {getItem: () => null, setItem(){}};
vm.createContext(ctx);
for (const f of FILES) vm.runInContext(fs.readFileSync(f, "utf8"), ctx, {filename: f});
"""


def _node(body: str) -> dict:
    files = [str(JS / n) for n in ("util.js", "ui.js", "render.js")]
    program = f"const FILES = {json.dumps(files)};\n{_STUB}\n{body}"
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True,  # noqa: S603
                         timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


HOSTED = _build(charges={"model_usd": 0.066, "calls": 5, "credits": 2400})
DONE = {"kind": "done", "reply": "Two vendors. Report saved.", "tools": [], "gate": None,
        "iterations": 3, "latency_ms": 12300, "model": SONNET, "receipt": HOSTED}


def _text(html: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&middot;", "·").strip()


@needs_node
def test_the_receipt_line_renders_live_and_reopened():
    got = _node(f"""
    const pending = {{role: "waku", pending: true, stream: ""}};
    vm.runInContext("applyStreamEvent", ctx)(pending, {json.dumps(DONE)});
    const card = vm.runInContext("chatTurnCard", ctx)(pending);
    const reopened = vm.runInContext("chatTurnCard", ctx)(vm.runInContext("histItem", ctx)(
      {{role: "assistant", content: "Two vendors. Report saved.",
        meta: {{model: "{SONNET}", iterations: 3, latency_ms: 12300,
                receipt: {json.dumps(HOSTED)}}}}}));
    const parts = vm.runInContext("receiptParts", ctx)({json.dumps(HOSTED)});
    const local = vm.runInContext("receiptParts", ctx)({json.dumps(EXPECTED)});
    console.log(JSON.stringify({{card, reopened, parts, local}}));""")
    assert got["parts"] == ["claude-sonnet-5 · 12.4k in / 1.9k out · $0.066", "treg 2 · $0.030",
                            "memory 4 used · 2 kept · report saved", "$0.096 · 2,400 credits"]
    assert got["local"][0].endswith("$0.064 est") and got["local"][-1] == "$0.094"
    for html in (got["card"], got["reopened"]):
        line = html[html.index('class="receipt"'):]
        assert 'onclick="toggleReceipt(this)"' in line and 'aria-expanded="false"' in line
        assert "2,400 credits" in line
        assert 'href="https://www.waku.one/memories/mem-a"' in line
        assert 'href="https://www.waku.one/matches"' in line, "no trace id: the list of matches"
        # the stats toggle hides .tele; the receipt is not inside it
        assert 'class="receipt' in html and "tele receipt" not in html
    assert "12.3s · 3 iter" in _text(got["card"])
    assert "tools $" not in got["card"], "the receipt carries the cost, not the old footer"


@needs_node
def test_a_turn_with_no_tools_and_no_memory_leaves_those_parts_out():
    bare = _build([("llm", {"usage": {"in": 900, "out": 40}})], used=0)
    got = _node(f"""console.log(JSON.stringify(
      {{parts: vm.runInContext("receiptParts", ctx)({json.dumps(bare)})}}));""")
    assert got["parts"] == ["claude-sonnet-5 · 900 in / 40 out · $0.003 est", "$0.003"]


# --- the kept facts Waku Memory did not take ----------------------------------

# 2026-10-04 on agent.waku.one: Waku Memory refused every send with "Session
# not found", and the card still listed six facts under "Kept in memory".
REFUSED = {"new_facts": 2, "kept": [
    {"subject": "Sean", "content": "Sean films on Fridays.", "project": None,
     "memory_id": None, "sent": False},
    {"subject": "Sean", "content": "Sean prices below Mem0.", "project": None,
     "memory_id": None, "sent": False}]}
HALF = {"new_facts": 2, "kept": [{**REFUSED["kept"][0], "memory_id": "mem-a", "sent": True},
                                 REFUSED["kept"][1]]}


@needs_node
def test_the_card_never_says_kept_in_memory_for_facts_waku_memory_refused():
    refused = _build([("consolidation", REFUSED)])
    half = _build([("consolidation", HALF)])
    got = _node(f"""
    const kept = vm.runInContext("keptList", ctx);
    const parts = vm.runInContext("receiptParts", ctx);
    const rows = vm.runInContext("receiptRows", ctx);
    console.log(JSON.stringify({{
      refused: kept({json.dumps(REFUSED)}), half: kept({json.dumps(HALF)}),
      sent: kept({json.dumps(EVENTS[-1][1])}),
      refusedLine: parts({json.dumps(refused)}), halfLine: parts({json.dumps(half)}),
      refusedRows: JSON.stringify(rows({json.dumps(refused)}))}}));""")
    assert "Kept in memory" not in got["refused"]
    assert "Kept on this agent only" in got["refused"] and 'class="kept kept-failed"' in got["refused"]
    assert "Waku Memory did not answer" in _text(got["refused"])
    assert "sends them again" in _text(got["refused"])
    assert "Kept in memory" in got["half"] and "1 of these 2 facts" in _text(got["half"])
    assert "kept-failed" not in got["sent"] and "did not answer" not in got["sent"]
    assert "memory 4 used · 2 kept, 2 on this agent only" in got["refusedLine"]
    assert "memory 4 used · 2 kept, 1 on this agent only" in got["halfLine"]
    assert "2 on this agent only" in got["refusedRows"]
