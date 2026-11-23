"""The dashboard answers only to this machine's own names (DNS rebinding).

same_origin() refuses a page from another site, because the browser puts that
site in Origin. DNS rebinding gets around it: the attacker's name, say
evil.test, is first served from the attacker's machine and then made to mean
127.0.0.1, so the browser sends this dashboard `Host: evil.test` and
`Origin: evil.test`. Those match, and same_origin() lets the request through.
A request that carries /plugin install would then run on the person's machine.
host_allowed() asks a different question: is Host one of our own names?

Offline: a real server on a free loopback port, and requests with chosen Host
headers over a plain socket client.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from waku.ops import dashboard


@pytest.mark.parametrize("host", [
    "localhost", "localhost:7777", "127.0.0.1", "127.0.0.1:7778", "[::1]", "[::1]:7777",
    "LOCALHOST:7777", None, "",
])
def test_a_loopback_dashboard_answers_to_its_own_names(monkeypatch, host):
    monkeypatch.delenv("WAKU_DASHBOARD_HOST", raising=False)
    assert dashboard.host_allowed(host) is True


@pytest.mark.parametrize("host", [
    "evil.test", "evil.test:7777", "evil.com", "localhost.evil.com", "localhost.evil.com:7777",
    "127.0.0.1.evil.com", "192.168.1.5:7777", "0.0.0.0:7777", "[2001:db8::1]:7777",
])
def test_a_loopback_dashboard_refuses_any_other_name(monkeypatch, host):
    monkeypatch.delenv("WAKU_DASHBOARD_HOST", raising=False)
    assert dashboard.host_allowed(host) is False


@pytest.mark.parametrize("bound", ["0.0.0.0", "10.0.0.7"])
def test_a_dashboard_bound_elsewhere_is_not_checked(monkeypatch, bound):
    """A hosted container binds 0.0.0.0 behind the gateway, which checks the
    Host itself, and sees `<tenant>.agent.waku.one`. Refusing that would
    take every tenant down."""
    monkeypatch.setenv("WAKU_DASHBOARD_HOST", bound)
    assert dashboard.host_allowed("sean.agent.waku.one") is True
    assert dashboard.host_allowed("evil.test") is True


def test_same_origin_alone_passes_the_rebinding_shape():
    """The gap this file closes, stated as a fact so nobody removes
    host_allowed() because same_origin() looks sufficient."""
    assert dashboard.same_origin("http://evil.test:7777", "evil.test:7777") is True


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.delenv("WAKU_DASHBOARD_HOST", raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, method: str, path: str, host: str, origin: str | None = None, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host, "Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    return response.status, response.read().decode()


def test_a_rebinding_post_is_refused_and_the_same_post_from_the_page_is_not(server):
    """The request I reproduced against the reviewed code: `/plugin list` with
    Host and Origin both evil.test. Before this check it answered with the
    reply. A harmless command stands in for an install."""
    payload = {"message": "/plugin list"}
    status, text = _request(server, "POST", "/api/chat", f"evil.test:{server}",
                            origin=f"http://evil.test:{server}", body=payload)
    assert status == 403 and "not this machine" in text
    assert "No plugins installed" not in text

    status, text = _request(server, "POST", "/api/chat", f"localhost:{server}",
                            origin=f"http://localhost:{server}", body=payload)
    assert status == 200 and "No plugins installed" in text


def test_a_rebinding_read_is_refused_too(server):
    """Rebinding lets the page read as well as write: /api/data holds the chat
    log, the facts and the settings."""
    status, _ = _request(server, "GET", "/api/data", f"evil.test:{server}")
    assert status == 403
    status, _ = _request(server, "GET", "/", f"evil.test:{server}")
    assert status == 403
    status, _ = _request(server, "GET", "/", f"127.0.0.1:{server}")
    assert status == 200
