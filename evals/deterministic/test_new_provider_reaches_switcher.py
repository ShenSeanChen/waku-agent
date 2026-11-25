"""A provider whose key is added after you curated your models still shows up.

The chat switcher shows the shortlist in .waku/models.json. That list is
written the first time you pin something, so a platform keyed later (Groq,
Amazon Bedrock) used to read "enabled" on the Models page and offer nothing
in the switcher. Now a keyed provider with no pin shows its default pair, and
the Disable button is how a provider leaves the switcher.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    for name in ("GROQ_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _save(home, specs):
    (home / "models.json").write_text(json.dumps({"pinned": specs}))


def test_a_newly_keyed_platform_appears_after_curation(home, monkeypatch):
    from waku.loop.models import PROVIDERS
    from waku.ops import catalog

    _save(home, ["anthropic:claude-opus-4-8"])
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-not-a-key")

    specs = catalog.pinned_specs()

    assert specs[0] == "anthropic:claude-opus-4-8"
    assert [s for s in specs if s.startswith("bedrock:")] == [
        f"bedrock:{m}" for m in PROVIDERS["bedrock"].default_pair()]


def test_a_provider_you_pinned_keeps_only_your_pins(home, monkeypatch):
    from waku.ops import catalog

    _save(home, ["bedrock:mistral.devstral-2-123b"])
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-not-a-key")

    assert [s for s in catalog.pinned_specs() if s.startswith("bedrock:")] == [
        "bedrock:mistral.devstral-2-123b"]


def test_a_provider_without_a_key_stays_out(home):
    from waku.ops import catalog

    _save(home, ["anthropic:claude-opus-4-8"])

    assert not any(s.startswith("bedrock:") for s in catalog.pinned_specs())


def test_the_defaults_are_not_written_to_models_json(home, monkeypatch):
    from waku.ops import catalog

    _save(home, ["anthropic:claude-opus-4-8"])
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-not-a-key")
    catalog.pinned_specs()

    assert json.loads((home / "models.json").read_text())["pinned"] == ["anthropic:claude-opus-4-8"]
