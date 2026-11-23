"""DETERMINISTIC EVAL -- the first open after an idle stop or `upgrade.sh --now`.

Seen three times in rehearsal: the first open of a tenant, on agent.waku.one
or inside waku.one's chat frame, showed "Your assistant is taking too long to
start. Try again." and a click twenty seconds later worked. Two causes, both
pinned here:

  the port      the spawner answers once Docker has started the container,
                while the dashboard inside has not bound its port. The
                gateway sent at once, was refused, re-listed, sent again at
                once and gave up. It now waits for the port
                (forward.listening, up to idle.READY_TIMEOUT_SECONDS).
  the budget    15 seconds for the spawner's provision-then-start, which a
                cold page cache passed. It is 60 now, and a page navigation
                that outlasts it gets a page that reloads itself instead of a
                dead end.
"""

from __future__ import annotations

import asyncio
import threading

from gatewaylib import signed_in_on_the_tenant_host

from hosted.core import idle
from hosted.gateway import answers

HTML = {"Accept": "text/html,application/xhtml+xml"}
JSON = {"Accept": "application/json"}


def _restart_later(container, port: int, seconds: float) -> threading.Timer:
    timer = threading.Timer(seconds, container.start, kwargs={"port": port})
    timer.start()
    return timer


def test_a_container_that_binds_its_port_late_is_waited_for(wired):
    """The tenant was stopped for idle; the next open starts it, and the
    dashboard binds two fifths of a second after the spawner answers. The
    request waits and gets the dashboard's answer, not the sentence."""
    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        await wired.launcher.stop(tenant_id)          # the idle stop
        port = wired.container.port
        wired.container.stop()                        # nothing listens yet
        timer = _restart_later(wired.container, port, 0.4)
        answer = await wired.send("GET", "/api/data", host=host, cookie=cookie,
                                  headers=JSON)
        timer.join()
        await wired.stop()
        return answer

    answer = asyncio.run(run())
    assert answer[0] == 200, answer
    assert wired.container.targets[-1] == "/api/data"


def test_a_page_whose_container_never_listens_gets_the_starting_page(wired, monkeypatch):
    """Past the wait, a page navigation is told the assistant is starting and
    reloads itself; a fetch still gets the sentence."""
    monkeypatch.setattr(idle, "READY_TIMEOUT_SECONDS", 0.3)

    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        await wired.launcher.stop(tenant_id)
        wired.container.stop()
        page = await wired.send("GET", "/", host=host, cookie=cookie, headers=HTML)
        await wired.launcher.stop(tenant_id)
        fetch = await wired.send("GET", "/api/data", host=host, cookie=cookie, headers=JSON)
        await wired.stop()
        return page, fetch

    page, fetch = asyncio.run(run())
    assert page[0] == answers.STARTING_STATUS
    assert page[1]["retry-after"] == str(idle.STARTING_RELOAD_SECONDS)
    body = page[2].decode()
    assert idle.STARTING_MESSAGE in body
    assert f'http-equiv="refresh" content="{idle.STARTING_RELOAD_SECONDS}; url=/"' in body
    assert idle.START_TIMEOUT_MESSAGE not in body
    assert fetch[0] != 200 and idle.START_TIMEOUT_MESSAGE in fetch[2].decode()


def test_a_start_that_outlasts_the_budget_shows_the_starting_page(wired, monkeypatch):
    monkeypatch.setattr(idle, "START_TIMEOUT_SECONDS", 0.1)

    async def run():
        await wired.start()
        host, cookie = await signed_in_on_the_tenant_host(wired)
        tenant_id = host.split(".", 1)[0]
        await wired.launcher.stop(tenant_id)
        wired.spawner.start_delay = 0.5
        page = await wired.send("GET", "/embed/chat?theme=dark", host=host, cookie=cookie,
                                headers=HTML)
        fetch = await wired.send("GET", "/api/data", host=host, cookie=cookie, headers=JSON)
        await wired.stop()
        return page, fetch

    page, fetch = asyncio.run(run())
    assert page[0] == answers.STARTING_STATUS
    # it reloads the page it was asked for: the chat inside the frame, theme kept
    assert "url=/embed/chat?theme=dark" in page[2].decode()
    assert idle.START_TIMEOUT_MESSAGE in fetch[2].decode()


def test_the_starting_page_only_ever_reloads_this_origin():
    for path, want in (("/", "/"), ("/embed/chat?theme=light", "/embed/chat?theme=light"),
                       ("//evil.example/", "/"), ("https://evil.example/", "/")):
        body = answers.starting_page(path).text
        assert f'url={want}"' in body, path


def test_the_start_budget_outlasts_a_cold_start_and_the_spawner_ask_outlasts_it():
    from hosted.gateway.spawner_client import START_ASK_TIMEOUT

    assert idle.START_TIMEOUT_SECONDS >= 60
    assert START_ASK_TIMEOUT > idle.START_TIMEOUT_SECONDS
