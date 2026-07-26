"""DETERMINISTIC EVAL — the curated model shortlist ("Your models").

Feature: a user pins models across providers; the chat switcher shows exactly
that shortlist, and the FIRST pinned model per provider is that provider's
default (adopted when you switch to it). Live goal Sean asked for: "a default
model for each api key, the user can choose more models, and the chat switcher
shows the models already selected in settings."

The shortlist lives in .waku/models.json as {"pinned": ["provider:model", ...]};
these tests drive the same helpers the dashboard's /api/pin route calls."""

from __future__ import annotations

import json

import pytest

from waku.ops import dashboard as d

PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                 "MINIMAX_API_KEY", "MOONSHOT_API_KEY", "ZHIPU_API_KEY", "OPENROUTER_API_KEY",
                 "XAI_API_KEY")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every load_settings() at a throwaway home, run from there so
    apply_settings's find_dotenv writes to a throwaway .env, and clear all
    provider keys so the default shortlist is empty unless a test sets one."""
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("")
    for var in PROVIDER_KEYS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_pin_persists_and_marks_first_per_provider_default(home):
    d.pin_action({"action": "pin", "provider": "gemini", "model": "gemini-3.5-flash"})
    d.pin_action({"action": "pin", "provider": "gemini", "model": "gemini-3.5-pro"})
    info = d.pin_action({"action": "pin", "provider": "kimi", "model": "kimi-k3"})

    # persisted to disk in insertion order
    saved = json.loads((home / "models.json").read_text())["pinned"]
    assert saved == ["gemini:gemini-3.5-flash", "gemini:gemini-3.5-pro", "kimi:kimi-k3"]

    # settings_info() surfaces the shortlist; first-per-provider is the default
    flags = {(p["provider"], p["model"]): p["default"] for p in info["pinned"]}
    assert flags[("gemini", "gemini-3.5-flash")] is True
    assert flags[("gemini", "gemini-3.5-pro")] is False
    assert flags[("kimi", "kimi-k3")] is True


def test_default_model_for_reads_first_pinned(home):
    assert d.default_model_for("kimi") == ""          # nothing pinned yet
    d.pin_action({"action": "pin", "provider": "kimi", "model": "kimi-k3"})
    d.pin_action({"action": "pin", "provider": "kimi", "model": "kimi-k2.6"})
    assert d.default_model_for("kimi") == "kimi-k3"   # the first one


def test_make_default_moves_model_to_front_of_its_group(home):
    d.pin_action({"action": "pin", "provider": "kimi", "model": "kimi-k3"})
    d.pin_action({"action": "pin", "provider": "kimi", "model": "kimi-k2.6"})
    d.pin_action({"action": "default", "provider": "kimi", "model": "kimi-k2.6"})
    assert d.default_model_for("kimi") == "kimi-k2.6"


def test_unpin_removes_and_promotes_next_default(home):
    d.pin_action({"action": "pin", "provider": "gemini", "model": "gemini-3.5-flash"})
    d.pin_action({"action": "pin", "provider": "gemini", "model": "gemini-3.5-pro"})
    info = d.pin_action({"action": "unpin", "provider": "gemini", "model": "gemini-3.5-flash"})
    assert [p["model"] for p in info["pinned"]] == ["gemini-3.5-pro"]
    assert d.default_model_for("gemini") == "gemini-3.5-pro"   # survivor is now default


def test_switching_provider_adopts_its_pinned_default(home, monkeypatch):
    """apply_settings on a provider change uses that provider's pinned default,
    never carrying the previous provider's model across endpoints (the live
    kimi->gemini 404)."""
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")
    (home / "models.json").write_text(json.dumps({"pinned": ["kimi:kimi-k3"]}))
    # start on gemini with a gemini model, then switch to kimi without naming one
    d.apply_settings({"provider": "gemini", "model": "gemini-3.5-flash", "keys": {}})
    info = d.apply_settings({"provider": "kimi", "keys": {}})
    assert info["provider"] == "kimi"
    assert info["model"] == "kimi-k3"          # adopted the pinned default, not gemini's model


def test_pinned_are_grouped_by_provider_for_display(home):
    """A model added later (e.g. claude-fable-5) should list WITH its provider's
    other models, not stranded at the bottom — while each provider's default
    (first pinned) stays on top."""
    (home / "models.json").write_text(json.dumps({"pinned": [
        "anthropic:claude-opus-4-8", "openai:gpt-5.3-chat-latest",
        "anthropic:claude-fable-5", "openai:gpt-4.1-mini"]}))
    rows = [(r["provider"], r["model"], r["default"]) for r in d.settings_info()["pinned"]]
    assert rows == [
        ("anthropic", "claude-opus-4-8", True),      # default stays first
        ("anthropic", "claude-fable-5", False),      # grouped with anthropic, not stranded
        ("openai", "gpt-5.3-chat-latest", True),
        ("openai", "gpt-4.1-mini", False),
    ]


def test_no_pins_is_empty_not_error(home):
    info = d.settings_info()
    assert info["pinned"] == []
    assert d.default_model_for("anthropic") == ""


def test_default_pair_is_flagship_then_fast(home):
    """Each provider ships a flagship + fast default pair for the switcher."""
    from waku.loop.models import PROVIDERS

    assert PROVIDERS["anthropic"].default_pair() == ["claude-opus-4-8", "claude-sonnet-5"]
    assert PROVIDERS["gemini"].default_pair() == ["gemini-3.1-pro-preview", "gemini-3.5-flash"]
    assert PROVIDERS["kimi"].default_pair() == ["kimi-k3", "kimi-k2.7-code-highspeed"]
    # a provider that never set flagship/fast falls back to model/small_model
    assert PROVIDERS["minimax"].default_pair() == ["MiniMax-M3", "MiniMax-M2"]


def test_defaults_apply_before_curation_and_only_for_keyed_providers(home, monkeypatch):
    """No models.json yet -> the switcher shows flagship+fast for providers that
    have a key set, flagship first (so it's the default). Providers without a
    key stay out (you can't use them)."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")      # only kimi is keyed
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")     # and anthropic

    info = d.settings_info()
    pairs = [(p["provider"], p["model"], p["default"]) for p in info["pinned"]]
    assert pairs == [
        ("anthropic", "claude-opus-4-8", True), ("anthropic", "claude-sonnet-5", False),
        ("kimi", "kimi-k3", True), ("kimi", "kimi-k2.7-code-highspeed", False),
    ]
    assert d.default_model_for("kimi") == "kimi-k3"        # flagship is the default
    assert d.default_model_for("gemini") == ""            # unkeyed -> no default


def test_pinning_snapshots_defaults_then_diverges(home, monkeypatch):
    """The first pin action persists the computed defaults + the change, so
    later edits don't keep resurrecting defaults."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "k")      # only kimi keyed -> 2 defaults
    d.pin_action({"action": "unpin", "provider": "kimi", "model": "kimi-k2.7-code-highspeed"})
    assert (home / "models.json").exists()               # now materialized
    assert [p["model"] for p in d.settings_info()["pinned"]] == ["kimi-k3"]


def test_known_catalog_providers_can_list(home):
    """Guard against the 'only 2 models' bug: a provider lists models from an
    explicit catalog_url OR a {base_url}/models endpoint (openai-wire only).
    openai has no base_url by default, so it MUST set catalog_url — without it
    the picker fell back to just its 2 hardcoded defaults.

    minimax/glm are anthropic-wire with no verified public /models endpoint, so
    they intentionally show their curated defaults until we wire+verify one."""
    from waku.loop.models import PROVIDERS

    CAN_LIST = {"anthropic", "openai", "openrouter", "gemini", "deepseek", "kimi", "xai"}
    for name in CAN_LIST:
        prov = PROVIDERS[name]
        can_list = bool(prov.catalog_url) or (prov.kind == "openai" and bool(prov.base_url))
        assert can_list, f"{name} lost its catalog source (add catalog_url)"


def test_list_models_honors_provider_override(home, monkeypatch):
    """The add-row picks a provider first, so list_models(provider) must list
    THAT provider's catalog, not the active one. Cache-seeded to avoid network."""
    import time

    from waku.loop.models import PROVIDERS

    url = PROVIDERS["kimi"].catalog_url
    # cache tuple is (ts, models, error) — None error means a real listing
    monkeypatch.setattr(d, "_models_cache", {url: (time.time(), [{"id": "kimi-k3"}], None)})
    out = d.list_models("kimi")
    assert out["provider"] == "kimi"
    assert out["listed"] is True
    assert [m["id"] for m in out["models"]] == ["kimi-k3"]
