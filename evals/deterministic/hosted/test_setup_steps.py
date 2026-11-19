"""DETERMINISTIC EVAL -- the setup page's three steps (spec 026 task C4, "The
two-minute path").

GET https://agent.waku.one/setup shows three steps and which are done:

  1. Sign in
  2. Add to Slack
  3. Mention @Waku

The gateway renders the page for the person whose session the browser holds.
The page has no script: each step's state is in the HTML the gateway sends.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path

import pytest
from gatewaylib import (
    APEX,
    SLACK_BOT_TOKEN,
    FakeSlack,
    FakeSpawner,
    Harness,
    sign_in,
)

from hosted.gateway.app import STATIC_FILES

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "hosted" / "gateway" / "static"
SETUP_HTML = ROOT / "hosted" / "templates" / "setup.html"
SETUP_CSS = STATIC / "setup.css"
COOKIE = "__Host-waku_session"
STEPS = ("sign-in", "slack", "mention")


@pytest.fixture
def slack():
    fake = FakeSlack()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def harness(tmp_path, slack):
    built = Harness(tmp_path, FakeSpawner())
    built.use_slack(slack)
    return built


def _run(harness: Harness, body):
    async def run():
        await harness.start()
        try:
            return await body()
        finally:
            await harness.stop()

    return asyncio.run(run())


async def _page(harness: Harness, cookie: str | None = None, query: str = ""):
    status, headers, body = await harness.send(
        "GET", "/setup" + query, host=APEX,
        cookie=f"{COOKIE}={cookie}" if cookie else None,
        headers={"Accept": "text/html"})
    return status, headers, body.decode("utf-8")


async def _connect(harness: Harness, cookie: str) -> None:
    _, headers, _ = await harness.send("GET", "/slack/install", host=APEX,
                                       cookie=f"{COOKIE}={cookie}")
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["location"]).query)["state"][0]
    status, headers, _ = await harness.send(
        "GET", f"/slack/oauth/callback?code=c&state={state}", host=APEX,
        cookie=f"{COOKIE}={cookie}")
    assert (status, headers["location"]) == (302, "/setup?slack=connected")


def states(page: str) -> dict[str, str]:
    """Each step's state, in the order the page shows them."""
    return dict(re.findall(r'<li data-step="([a-z-]+)" data-state="([a-z]+)"', page))


def links(page: str) -> list[str]:
    return re.findall(r'<a [^>]*href="([^"]+)"', page)


def test_a_visitor_who_is_not_signed_in_sees_three_steps_and_none_done(harness):
    status, _, page = _run(harness, lambda: _page(harness))
    assert status == 200
    assert states(page) == {"sign-in": "todo", "slack": "todo", "mention": "todo"}
    assert list(states(page)) == list(STEPS)
    # The one thing to do is sign in. Slack cannot be added before that.
    assert links(page) == ["/login"]


def test_a_signed_in_person_is_offered_add_to_slack(harness):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie)

    _, _, page = _run(harness, body)
    assert states(page) == {"sign-in": "done", "slack": "todo", "mention": "todo"}
    assert links(page) == ["/slack/install"]
    assert "Add to Slack" in page


def test_a_person_who_connected_slack_sees_every_step_done_and_their_workspace(harness):
    async def body():
        tenant, cookie, _ = await sign_in(harness)
        await _connect(harness, cookie)
        return tenant, await _page(harness, cookie, "?slack=connected")

    tenant, (_, _, page) = _run(harness, body)
    assert states(page) == {"sign-in": "done", "slack": "done", "mention": "done"}
    # The workspace's name is Slack's text, so it is escaped. FakeSlack's is
    # "Ana & Co".
    assert "Ana &amp; Co" in page and "Ana & Co" not in page
    assert "Slack is connected." in page
    # Two ways back to Slack's approval page: the same workspace approved
    # again comes first and is the main button, a different one is the quiet one.
    assert links(page) == ["/slack/install", "/slack/install", f"https://{tenant}.{APEX}/"]
    assert re.findall(r'<a data-slot="button"( class="secondary")? href="/slack/install">([^<]+)</a>',
                      page) == [("", "Update permissions"),
                                (' class="secondary"', "Connect a different workspace")]
    assert SLACK_BOT_TOKEN not in page


def test_a_workspace_name_cannot_put_markup_in_the_page(harness, slack):
    slack.oauth_answer = {**slack.oauth_answer,
                          "team": {"id": "T0ANA", "name": '<script>alert(1)</script>"'}}

    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        await _connect(harness, cookie)
        return await _page(harness, cookie)

    _, _, page = _run(harness, body)
    assert "<script" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot;" in page


@pytest.mark.parametrize("outcome, sentence", [
    ("connected", "Slack is connected."),
    ("denied", "Slack was not connected, because the request was cancelled in Slack."),
    ("expired", "That link expired before Slack answered. Press Add to Slack again."),
    ("failed", "Slack did not finish the install. Press Add to Slack again."),
    ("taken", "That workspace is already connected to another Waku account."),
])
def test_the_page_says_how_the_install_ended(harness, outcome, sentence):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie, f"?slack={outcome}")

    _, _, page = _run(harness, body)
    assert sentence in page


@pytest.mark.parametrize("query", ["?slack=%3Cscript%3Ealert(1)%3C/script%3E",
                                   "?slack=CONNECTED", "?slack=", "?other=connected"])
def test_the_page_shows_nothing_from_a_query_it_does_not_know(harness, query):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie), await _page(harness, cookie, query)

    (status, _, plain), (_, _, with_query) = _run(harness, body)
    assert status == 200 and states(plain)["sign-in"] == "done"
    assert with_query == plain
    assert "alert(1)" not in with_query


def test_without_slack_configured_the_page_offers_no_install(tmp_path):
    harness = Harness(tmp_path, FakeSpawner())      # no use_slack: Slack is off

    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie)

    _, _, page = _run(harness, body)
    assert states(page) == {"sign-in": "done", "slack": "off", "mention": "todo"}
    assert links(page) == []
    assert "Slack is not available on this server yet." in page
    # The operator reads the missing names in the 503 and in the log. A
    # visitor is not shown the server's configuration.
    assert "WAKU_SLACK" not in page


def test_a_disabled_persons_session_is_not_signed_in(harness):
    async def body():
        tenant, cookie, _ = await sign_in(harness)
        harness.store.set_status(tenant, "disabled")
        return await _page(harness, cookie)

    _, _, page = _run(harness, body)
    assert states(page)["sign-in"] == "todo"


def test_the_page_runs_no_script_and_is_never_kept_or_framed(harness):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie)

    _, headers, page = _run(harness, body)
    policy = headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src" not in policy          # so default-src 'none' covers scripts
    assert "frame-ancestors 'none'" in policy
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert "<script" not in page
    assert headers["content-type"].startswith("text/html")


def test_the_setup_path_is_only_a_get_on_the_apex(harness):
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        posted = await harness.json_post("/setup", {}, host=APEX, cookie=f"{COOKIE}={cookie}")
        return posted[0]

    assert _run(harness, body) == 404


# --- the page's files, by the sign-in page's rules --------------------------

def test_every_asset_the_setup_page_names_is_served():
    named = set(re.findall(r"/auth/static/([A-Za-z0-9._/-]+)",
                           SETUP_HTML.read_text(encoding="utf-8")))
    assert "setup.css" in named
    missing = sorted(name for name in named if name not in STATIC_FILES)
    assert not missing, f"the page names files the gateway will not serve: {missing}"
    assert (STATIC / STATIC_FILES["setup.css"][0]).is_file()


def test_the_design_system_loads_before_the_setup_stylesheet():
    order = re.findall(r'href="/auth/static/([^"]+)"', SETUP_HTML.read_text(encoding="utf-8"))
    for earlier in ("design/tokens.css", "design/type.css", "design/fonts.css",
                    "design/controls.css", "login.css"):
        assert order.index(earlier) < order.index("setup.css"), earlier


def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", SETUP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_the_setup_stylesheet_takes_every_value_from_a_token():
    """docs/context/design-system.md: colour, size, face and corner come from
    tokens, and nothing has a shadow."""
    css = _css()
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css)
    sizes = re.findall(r"font-size:\s*([^;]+);", css)
    assert sizes and all("var(--text-" in value for value in sizes)
    faces = re.findall(r"font-family:\s*([^;]+);", css)
    assert faces and all("var(--face-" in value or value == "inherit" for value in faces)
    assert not re.findall(r"\b\d+px\b", css), "a pixel value bypasses the tokens"
    assert "box-shadow" not in css
    assert "border-radius" not in css or "var(--" in css


def test_the_add_to_slack_link_is_a_control(harness):
    """controls.css gives the focus ring and the shape to [data-slot]. The
    link that starts the install carries it, so a keyboard user can see it."""
    async def body():
        _tenant, cookie, _ = await sign_in(harness)
        return await _page(harness, cookie)

    _, _, page = _run(harness, body)
    assert re.search(r'<a [^>]*data-slot="button"[^>]*href="/slack/install"', page) or \
        re.search(r'<a [^>]*href="/slack/install"[^>]*data-slot="button"', page)
