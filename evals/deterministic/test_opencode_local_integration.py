"""Local OpenCode configuration/catalog checks, with no server or provider calls."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from waku import integrations
from waku.config import Settings
from waku.loop import models, opencode_local
from waku.ops import browser_agent, catalog, pricing, settings_api

MUSE = "muse-spark-1.3-contributor-free"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / ".waku"))
    # A --basetemp below a developer's home must never discover its ancestor .env.
    monkeypatch.setattr(integrations, "_env_path", lambda: tmp_path / ".env")
    for name in ("WAKU_PROVIDER", "WAKU_API_KEY", "WAKU_MODEL", "WAKU_SMALL_MODEL",
                 "WAKU_BASE_URL", "WAKU_LLM_TIMEOUT", "OPENCODE_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    for provider in models.PROVIDERS.values():
        if provider.key_env:
            monkeypatch.delenv(provider.key_env, raising=False)
    monkeypatch.setattr(integrations, "_HEALTH", {})
    monkeypatch.setattr(catalog, "_models_cache", {})
    monkeypatch.setattr(pricing, "_price_cache", {})
    monkeypatch.setattr(browser_agent, "current", lambda: None)

    def no_network(*args, **kwargs):
        pytest.fail("unexpected network request")

    monkeypatch.setattr("urllib.request.urlopen", no_network)


@pytest.mark.parametrize(("scoped", "explicit", "timeout", "expected_url", "expected_timeout"), [
    (None, None, None, "http://127.0.0.1:4096", 1800),
    ("http://localhost:4100", None, None, "http://localhost:4100", 1800),
    ("http://localhost:4100", "http://localhost:4200", "90", "http://localhost:4200", 90),
    (None, None, "0", "http://127.0.0.1:4096", 0),
])
def test_local_client_defaults_and_overrides(
    monkeypatch, scoped, explicit, timeout, expected_url, expected_timeout
):
    captured = {}

    def client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(messages=None)

    monkeypatch.setattr(opencode_local, "OpenCodeLocalClient", client)
    if scoped:
        monkeypatch.setenv("OPENCODE_SERVER_URL", scoped)
    if timeout is not None:
        monkeypatch.setenv("WAKU_LLM_TIMEOUT", timeout)
    # An unrelated global override is neither required nor sent to OpenCode.
    settings = Settings(provider="opencode_local", api_key="unrelated→key", base_url=explicit)
    models.get_client(settings)

    assert captured == {"base_url": expected_url, "timeout": expected_timeout}
    assert settings.model == settings.small_model == MUSE


def test_global_base_url_override_and_explicit_model_are_preserved(monkeypatch):
    captured = {}
    monkeypatch.setattr(opencode_local, "OpenCodeLocalClient",
                        lambda **kwargs: captured.update(kwargs))
    monkeypatch.setenv("WAKU_PROVIDER", "opencode_local")
    monkeypatch.setenv("WAKU_BASE_URL", "http://localhost:4200")
    monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4100")
    monkeypatch.setenv("WAKU_MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setenv("WAKU_SMALL_MODEL", "gpt-5.5")
    settings = Settings()
    models.get_client(settings)

    assert captured["base_url"] == "http://localhost:4200"
    assert settings.model == "anthropic/claude-sonnet-5"
    assert settings.small_model == "gpt-5.5"


def _catalog_body():
    return {"all": [
        {"id": "opencode", "key": "must-not-be-exposed", "models": {
            MUSE: {"id": MUSE, "capabilities": {"toolcall": True, "reasoning": True},
                   "limit": {"context": 100000}, "cost": {"input": 0, "output": 0}},
            "paid-model": {"id": "paid-model", "cost": {"input": 0, "output": 2.5}},
            "unknown:free": {"id": "unknown:free"},
        }},
        {"id": "another", "models": {
            "model-a": {"id": "model-a", "cost": {"input": 1.25, "output": 5}},
        }},
        {"id": "disconnected", "models": {"not-accessible": {"id": "not-accessible"}}},
    ], "connected": ["opencode", "another"], "default": {"opencode": "paid-model"}}


def _fake_catalog(monkeypatch, body):
    requests = []

    def urlopen(req, timeout):
        requests.append((req.full_url, dict(req.header_items()), timeout))
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests


@pytest.mark.parametrize("active", [False, True])
def test_catalog_uses_provider_api_and_connected_models_without_auth(monkeypatch, active):
    monkeypatch.setenv("WAKU_PROVIDER", "opencode_local" if active else "anthropic")
    monkeypatch.setenv("WAKU_API_KEY", "unrelated-secret")
    monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4100/")
    if not active:
        monkeypatch.setenv("WAKU_BASE_URL", "https://other.example")
        monkeypatch.setenv("WAKU_MODEL", "other-active-model")
    requests = _fake_catalog(monkeypatch, _catalog_body())

    result = catalog.list_models(None if active else "opencode_local")
    again = catalog.list_models("opencode_local")

    assert result == again
    assert len(requests) == 1
    url, headers, _ = requests[0]
    assert url == "http://localhost:4100/provider"
    assert not any(k.lower() in ("authorization", "x-api-key") for k in headers)
    assert result["listed"]
    assert result["model"] == result["small_model"] == MUSE
    entries = {model["id"]: model for model in result["models"]}
    assert set(entries) == {MUSE, "paid-model", "unknown:free", "another/model-a"}
    assert entries[MUSE]["free"] and entries[MUSE]["tools"]
    assert entries[MUSE]["context"] == 100000
    assert not entries["paid-model"]["free"]
    assert not entries["unknown:free"]["free"]
    assert entries["another/model-a"]["price_in"] == 1.25
    assert pricing.price_for("opencode_local", "opencode/paid-model") == (0, 2.5)
    assert pricing.price_for("opencode_local", "another/model-a") == (1.25, 5)
    assert pricing.price_for("other-provider", "another/model-a") != (1.25, 5)
    assert "must-not-be-exposed" not in json.dumps(result)


def test_settings_and_unselected_catalog_do_not_probe_local_server(monkeypatch):
    monkeypatch.setenv("WAKU_PROVIDER", "glm")
    assert catalog.list_models()["listed"] is False
    assert settings_api.settings_info()["pinned"] == []
    local = next(view for view in integrations.list_providers() if view.key == "opencode_local")
    assert local.status.state is integrations.IntegrationState.NOT_CONFIGURED
    assert not any(field.secret for field in local.fields)


def test_selected_local_defaults_are_pinned_without_a_probe(monkeypatch):
    monkeypatch.setenv("WAKU_PROVIDER", "opencode_local")
    result = settings_api.settings_info()
    assert result["pinned"] == [{"provider": "opencode_local", "model": MUSE, "default": True}]
    local = next(view for view in integrations.list_providers() if view.key == "opencode_local")
    assert local.status.state is integrations.IntegrationState.CONFIGURED


def test_local_catalog_failure_preserves_reason_and_caches_defaults(monkeypatch):
    calls = []

    def fail(req, timeout):
        calls.append(req.full_url)
        raise URLError("local server unavailable")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = catalog.list_models("opencode_local")
    assert catalog.list_models("opencode_local") == result
    assert len(calls) == 1
    assert not result["listed"]
    assert "local server unavailable" in result["error"]
    assert result["models"] == [{"id": MUSE}]


def test_local_catalog_rejects_openai_shape(monkeypatch):
    _fake_catalog(monkeypatch, {"data": [{"id": "not-an-opencode-catalog"}]})
    result = catalog.list_models("opencode_local")
    assert not result["listed"]
    assert "all and connected arrays" in result["error"]


def test_only_exact_muse_id_has_static_free_pricing():
    assert pricing.price_for("opencode_local", MUSE) == (0, 0)
    assert pricing.price_for("opencode_local", f"opencode/{MUSE}") == (0, 0)
    for model in ("muse-spark-1.3", "unknown:free", "another/muse-spark-1.3-contributor-free"):
        assert pricing.price_for("opencode_local", model) == pricing.PRICING["opencode_local"]


def test_select_local_provider_without_key_or_automatic_probe():
    result = integrations.apply_provider("opencode_local")
    assert result.ok
    assert result.view.status.state is integrations.IntegrationState.CONFIGURED
    assert os.environ["WAKU_PROVIDER"] == "opencode_local"
    assert os.environ["OPENCODE_SERVER_URL"] == "http://127.0.0.1:4096"
    assert not any(field.secret for field in result.view.fields)
    assert "API_KEY" not in Path(".env").read_text()


def test_explicit_local_connection_test_can_probe_without_key(monkeypatch):
    calls = _fake_catalog(monkeypatch, _catalog_body())
    result = integrations.test_integration("opencode_local")
    assert result.status.state is integrations.IntegrationState.CONNECTED
    assert calls[0][0] == "http://127.0.0.1:4096/provider"


@pytest.mark.parametrize("active", [False, True])
def test_saving_local_endpoint_probes_only_if_selected(monkeypatch, active):
    monkeypatch.setenv("WAKU_PROVIDER", "opencode_local" if active else "anthropic")
    calls = _fake_catalog(monkeypatch, _catalog_body())
    result = integrations.apply_provider("opencode_local", base_url="http://localhost:4100",
                                         activate=False)
    assert result.ok
    assert os.environ["OPENCODE_SERVER_URL"] == "http://localhost:4100"
    assert len(calls) == int(active)
    if active:
        assert calls[0][0] == "http://localhost:4100/provider"
        assert result.view.status.state is integrations.IntegrationState.CONNECTED


def test_local_provider_does_not_store_credentials():
    result = integrations.apply_provider("opencode_local", key="not-for-waku")
    assert not result.ok
    assert "credentials in OpenCode" in result.error
    assert not Path(".env").exists()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_local_picker_renders_endpoint_and_no_missing_key_prompt(monkeypatch):
    monkeypatch.setenv("WAKU_PROVIDER", "opencode_local")
    from dataclasses import asdict

    local = next(view for view in integrations.list_providers() if view.key == "opencode_local")
    script = Path(__file__).resolve().parents[2] / "waku/ops/static/js/models.js"
    javascript = """
const fs = require('fs'), vm = require('vm');
const p = JSON.parse(fs.readFileSync(0, 'utf8')), root = {innerHTML: ''};
const st = {provider: 'opencode_local', model: 'model', small_model: 'model'};
const context = {D: {providers: [p], settings: st}, esc: s => String(s),
                 document: {getElementById: () => root}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.loadModalModels = () => {};
context.openProviderModal('opencode_local');
process.stdout.write(JSON.stringify({html: root.innerHTML,
  status: context.providerCardStatus(p, st),
  keyed: context.providerCardStatus({key:'anthropic', fields:[{secret:true, configured:false}]}, st)}));
"""
    run = subprocess.run([shutil.which("node"), "-e", javascript, str(script)],
                         input=json.dumps(asdict(local)), capture_output=True, text=True,
                         timeout=30, check=True)
    result = json.loads(run.stdout)
    assert result["status"] == "enabled"
    assert result["keyed"] == "unconfigured"
    assert 'id="pm-key"' not in result["html"]
    assert 'type="text" id="pm-base-url"' in result["html"]
    assert "http://127.0.0.1:4096" in result["html"]
    assert "not set" not in result["html"]
