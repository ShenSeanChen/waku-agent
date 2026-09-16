"""A quota denial must not turn Waku's fail-open paths into repeated submissions."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from evals.helpers import make_waku
from waku.loop import opencode_local

MUSE = "muse-spark-1.3-contributor-free"


@pytest.mark.parametrize("graph_workflows", [False, True])
@pytest.mark.parametrize("qualified_main", [False, True])
def test_waku_surfaces_quota_without_resubmitting_after_gate_or_graph_fallback(
    monkeypatch, tmp_path, graph_workflows, qualified_main
):
    calls = []
    sessions = []

    def urlopen(request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        calls.append((method, path))
        if (method, path) == ("GET", "/experimental/tool/ids"):
            data = ["bash", "read"]
        elif (method, path) == ("POST", "/session"):
            sessions.append(f"ses_test_{len(sessions) + 1}")
            data = {"id": sessions[-1]}
        elif method == "GET" and path.endswith("/message"):
            data = []
        elif (method, path) == ("GET", "/session/status"):
            data = {sid: {"type": "retry", "action": {"reason": "free_tier_limit"},
                          "next": 2_000_000_000_000} for sid in sessions}
        elif method == "DELETE" or (method == "POST" and path.endswith(
            ("/prompt_async", "/abort")
        )):
            data = None
        else:
            raise AssertionError(f"Unexpected request: {method} {path}")
        return io.BytesIO(json.dumps(data).encode() if data is not None else b"")

    monkeypatch.setattr(opencode_local.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(opencode_local.urllib.request, "build_opener",
                        lambda *handlers: SimpleNamespace(open=urlopen))
    client = opencode_local.OpenCodeLocalClient()
    app = make_waku(
        tmp_path / "waku", client=client, provider="opencode_local",
        model=f"opencode/{MUSE}" if qualified_main else MUSE, small_model=MUSE,
        graph_workflows=graph_workflows, experimental=False,
        semantic_store="sqlite", episodic_store="sqlite",
    )
    events = []
    try:
        with pytest.raises(opencode_local.OpenCodeQuotaExceeded) as caught:
            app.respond("hello", observer=lambda kind, event: events.append(kind))
        assert caught.value.next_retry == 2_000_000_000_000
        assert "free_tier_limit" in str(caught.value)
        assert "gate" in events
        assert ("graph_start" in events) is graph_workflows
        assert len(sessions) == 1
        assert [path for method, path in calls
                if method == "POST" and path.endswith("/prompt_async")] == [
                    "/session/ses_test_1/prompt_async"]
        assert ("POST", "/session/ses_test_1/abort") in calls
        assert ("DELETE", "/session/ses_test_1") in calls
        assert not app.session.history, "a quota failure must not become a successful chat reply"
    finally:
        app.close()
        app.conn.close()
