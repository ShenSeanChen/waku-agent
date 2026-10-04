"""DETERMINISTIC EVAL -- signing in from the email signs in the tab that asked.

WHY THIS FILE EXISTS. On 2026-10-03 the sign-in page sent a magic link, the
link opened a NEW tab, that tab signed in, and the tab the person typed their
address into sat on "Check your email for the link." forever. login.js now
offers the single-use enter URL to the asking tab over a BroadcastChannel
before using it itself.

These run the real login.js in node, one vm context per tab, joined by a fake
BroadcastChannel that delivers asynchronously the way a browser's does. The
cases are the ones that matter for a code that works once:

  - an asking tab takes it: that tab navigates, the email tab does NOT and
    says so
  - nobody asks: the email tab navigates itself, as before
  - a URL that is not this gateway's enter URL is never navigated to by the
    asking tab
  - two asking tabs: exactly one navigates
  - no BroadcastChannel: today's behaviour, unchanged
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LOGIN_JS = ROOT / "hosted" / "gateway" / "static" / "login.js"

APEX = "agent.waku.one"
CODE = "A" * 21 + "b-_" + "9" * 19
GOOD = f"https://abcdefgh2345.{APEX}/auth/enter?code={CODE}"

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const source = fs.readFileSync(process.env.LOGIN_JS, "utf8");
const scenario = JSON.parse(process.env.SCENARIO);

// One hub per test: every channel of the same name hears every OTHER one,
// asynchronously, with the message cloned -- the browser's contract.
const hub = [];
function FakeChannel(name) {
  this.name = name; this.onmessage = null; this.closed = false;
  hub.push(this);
}
FakeChannel.prototype.postMessage = function (data) {
  const sender = this;
  const copy = JSON.parse(JSON.stringify(data));
  for (const other of hub) {
    if (other === sender || other.closed || other.name !== sender.name) continue;
    setTimeout(() => { if (!other.closed && other.onmessage) other.onmessage({data: copy}); }, 1);
  }
};
FakeChannel.prototype.close = function () { this.closed = true; };

function tab(name, hash, enter, broadcast) {
  const record = {name, assigned: [], status: "", closed: false};
  const handlers = {};
  const element = (id) => ({
    id, value: "you@example.com", disabled: false, textContent: "",
    classList: {toggle() {}},
    addEventListener(type, fn) { handlers[id + ":" + type] = fn; },
  });
  const elements = {status: element("status"), form: element("form"),
                    send: element("send"), email: element("email")};
  Object.defineProperty(elements.status, "textContent", {
    get() { return record.status; }, set(v) { record.status = v; }});
  const context = {
    document: {body: {dataset: {supabaseUrl: "https://sb.example", supabaseKey: "k"}},
               getElementById: (id) => elements[id]},
    location: {origin: "https://" + scenario.apex, hostname: scenario.apex,
               pathname: "/login", hash,
               assign: (u) => record.assigned.push(u)},
    history: {replaceState() {}},
    window: {close() { record.closed = true; }},
    fetch: async (url) => url === "/auth/session"
      ? {ok: true, json: async () => ({enter})}
      : {ok: true, json: async () => ({})},
    URL, URLSearchParams, Intl, Promise, Math, Date, JSON, String,
    setTimeout, clearTimeout, console,
  };
  if (broadcast) context.BroadcastChannel = FakeChannel;
  vm.runInNewContext(source, context);
  record.submit = () => handlers["form:submit"]({preventDefault() {}});
  return record;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const askers = [];
  for (let i = 0; i < scenario.askers; i++) {
    const asker = tab("asker" + i, "", null, scenario.broadcast);
    await asker.submit();
    askers.push(asker);
  }
  await sleep(10);
  const email = tab("email", "#access_token=t", scenario.enter, scenario.broadcast);
  await sleep(1200);
  const out = (r) => ({assigned: r.assigned, status: r.status, closed: r.closed});
  console.log(JSON.stringify({askers: askers.map(out), email: out(email)}));
})();
"""


def _run(*, enter: str = GOOD, askers: int = 1, broadcast: bool = True) -> dict:
    scenario = {"apex": APEX, "enter": enter, "askers": askers, "broadcast": broadcast}
    out = subprocess.run(  # noqa: S603 -- fixed argv, our own script
        [shutil.which("node"), "-e", HARNESS],
        env={"LOGIN_JS": str(LOGIN_JS), "SCENARIO": json.dumps(scenario)},
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


@needs_node
def test_the_asking_tab_takes_the_hand_off_and_the_email_tab_does_not_use_it():
    got = _run()
    assert got["askers"][0]["assigned"] == [GOOD]
    assert got["email"]["assigned"] == [], "the code is single-use; two tabs spent it"
    assert "other tab" in got["email"]["status"]
    assert got["email"]["closed"], "the email tab should at least try to close"


@needs_node
def test_with_no_asking_tab_the_email_tab_signs_itself_in():
    got = _run(askers=0)
    assert got["email"]["assigned"] == [GOOD]


@needs_node
@pytest.mark.parametrize("bad", [
    f"https://evil.example/auth/enter?code={CODE}",
    f"http://abcdefgh2345.{APEX}/auth/enter?code={CODE}",
    f"https://abcdefgh2345.{APEX}:8443/auth/enter?code={CODE}",
    f"https://a.abcdefgh2345.{APEX}/auth/enter?code={CODE}",
    f"https://ABCDEFGH2345.evil.example/auth/enter?code={CODE}",
    f"https://abcdefgh2345.{APEX}/settings?code={CODE}",
    f"https://abcdefgh2345.{APEX}/auth/enter?code={CODE}&next=//evil.example",
    f"https://user@abcdefgh2345.{APEX}/auth/enter?code={CODE}",
    f"https://{APEX}.evil.example/auth/enter?code={CODE}",
    "javascript:alert(1)",
], ids=["other-site", "http", "port", "two-labels", "other-apex", "other-path",
        "extra-query", "userinfo", "apex-as-prefix", "javascript"])
def test_an_enter_url_that_is_not_ours_is_never_taken(bad):
    got = _run(enter=bad)
    assert got["askers"][0]["assigned"] == [], f"the asking tab navigated to {bad}"
    # The email tab does what it always did with the server's answer.
    assert got["email"]["assigned"] == [bad]


@needs_node
def test_two_asking_tabs_exactly_one_navigates():
    got = _run(askers=2)
    navigated = [a["assigned"] for a in got["askers"] if a["assigned"]]
    assert navigated == [[GOOD]], got
    assert got["email"]["assigned"] == []


@needs_node
def test_without_broadcast_channel_nothing_changes():
    got = _run(broadcast=False)
    assert got["askers"][0]["assigned"] == []
    assert got["email"]["assigned"] == [GOOD]
    assert got["email"]["status"] == "Signing you in..."
