"""The first working key becomes the current provider.

WHY THIS EXISTS. The Models modal's save sends `activate=False` on purpose:
once you have a provider that works, adding a second one must not silently
move your turns onto it. That default is wrong in exactly one case, and that
case is a first run.

On 2026-09-28 a hosted tenant's WAKU_PROVIDER said `waku-platform` -- the free
tier, whose row had just been removed from the build -- so the setting named a
provider that no longer existed. They pasted a valid Anthropic key, the
dashboard showed it configured with a green dot, and every turn went nowhere
with nothing in any log. The key was never the problem; the setting was.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from waku import integrations


@pytest.fixture
def env(monkeypatch):
    """A clean environment whose writes go nowhere near a real .env."""
    for name in list(integrations.PROVIDERS):
        selected = integrations.PROVIDERS[name]
        monkeypatch.delenv(selected.key_env, raising=False)
    monkeypatch.delenv("WAKU_PROVIDER", raising=False)
    return monkeypatch


def test_a_setting_naming_a_provider_this_build_lacks_cannot_serve(env):
    """The exact shape of the live failure: a stored setting outlives the code
    that gave it meaning. A row removed from a release leaves everyone who had
    selected it pointing at nothing."""
    env.setenv("WAKU_PROVIDER", "waku-platform-that-was-removed")
    assert integrations._provider_can_serve("waku-platform-that-was-removed") is False


def test_a_provider_with_no_key_cannot_serve(env):
    """Named and present, but nothing to authenticate with. The other half of
    'usable', and the one a test that only checked membership would miss."""
    assert integrations._provider_can_serve("anthropic") is False


def test_a_provider_with_a_key_can_serve(env):
    env.setenv(integrations.PROVIDERS["anthropic"].key_env, "sk-test")
    assert integrations._provider_can_serve("anthropic") is True


def test_the_first_key_is_adopted_when_the_current_provider_is_a_ghost(env):
    """The live case. WAKU_PROVIDER names something gone; a key arrives for a
    real provider; that provider must become current even though the caller
    said activate=False."""
    env.setenv("WAKU_PROVIDER", "waku-platform")
    assert integrations._adoptable(integrations.PROVIDERS["anthropic"], "sk-new") is True


def test_the_first_key_is_adopted_when_nothing_was_ever_chosen(env):
    """A genuinely fresh install: no WAKU_PROVIDER at all."""
    assert integrations._adoptable(integrations.PROVIDERS["anthropic"], "sk-new") is True


def test_a_working_setup_is_never_hijacked(env):
    """THE HALF THAT PROTECTS PEOPLE. Adding a second provider to a working
    install must not move their turns onto it -- that is the whole reason the
    modal sends activate=False, and an adoption rule that ignored it would
    spend somebody's money on the wrong account."""
    env.setenv("WAKU_PROVIDER", "anthropic")
    env.setenv(integrations.PROVIDERS["anthropic"].key_env, "sk-working")
    assert integrations._adoptable(integrations.PROVIDERS["openai"], "sk-second") is False


def test_a_save_carrying_no_key_adopts_nothing(env):
    """Without this, a save that only sets a base URL would move a user from
    one provider that cannot serve a turn to another one that also cannot."""
    env.setenv("WAKU_PROVIDER", "waku-platform")
    assert integrations._adoptable(integrations.PROVIDERS["anthropic"], None) is False


def test_a_key_already_on_file_does_not_adopt_on_its_own(env):
    """THE RULE IS "the first key you ENTER", and this is where that wording
    earns its keep.

    An earlier draft adopted whenever the saved provider held a key by any
    means. That is a wider rule, and the existing suite caught it:
    test_saving_noncurrent_provider_does_not_activate_or_rebuild edits a base
    URL on a provider that holds a key while the current provider holds none,
    and expects the current provider to be left alone. It should be. The
    person is editing settings, not choosing a provider.

    Somebody stranded like this and not typing a key is recovered by the
    first-run gate, which asks instead of guessing.
    """
    env.setenv("WAKU_PROVIDER", "waku-platform")
    env.setenv(integrations.PROVIDERS["anthropic"].key_env, "sk-already-here")
    assert integrations._adoptable(integrations.PROVIDERS["anthropic"], None) is False


def test_the_gate_and_the_server_agree_on_what_configured_means(env):
    """`_provider_can_serve` uses bool(env[key_env]), which is the same test
    the Models grid colours a card with (integrations.py line ~272). Two
    definitions of "configured" is how a setup screen and a settings page come
    to disagree in front of a user."""
    text = pathlib.Path(integrations.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _provider_can_serve"):]
    body = body[:body.index("\ndef ")]
    assert "os.environ.get(selected.key_env" in body


# --- the wiring, which the helpers above do not prove ----------------------


def _isolated(monkeypatch, tmp_path):
    """apply_provider writes .env and probes a key. Neither belongs in a test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / ".waku"))
    monkeypatch.setattr(integrations, "_provider_probe", lambda values: None)
    from waku.ops import browser_agent
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)


def test_apply_provider_actually_adopts_despite_activate_false(monkeypatch, tmp_path):
    """THE WIRING, and it is a separate test on purpose.

    _adoptable and _provider_can_serve were both covered above, and deleting
    the two lines in apply_provider that CALL them still passed every one of
    those tests. A helper nothing invokes is a guard with no fixture -- the
    shape this project has now been caught by more than once -- so this drives
    the real entry point and reads the real result.

    This is the live 2026-09-28 case end to end: a setting naming a provider
    the build no longer has, and a key arriving from the Models modal, which
    always sends activate=False.
    """
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_PROVIDER", "waku-platform")
    monkeypatch.delenv(integrations.PROVIDERS["anthropic"].key_env, raising=False)

    result = integrations.apply_provider("anthropic", key="sk-first-key",
                                         activate=False)

    assert result.ok, result.error
    assert os.environ["WAKU_PROVIDER"] == "anthropic", (
        "apply_provider did not adopt the first key; the modal's "
        "activate=False left the user on a provider that cannot serve a turn")


def test_apply_provider_leaves_a_working_provider_alone(monkeypatch, tmp_path):
    """The other direction, through the same entry point. Adding a second
    provider to a working install must not move the user's turns onto it."""
    _isolated(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_PROVIDER", "anthropic")
    monkeypatch.setenv(integrations.PROVIDERS["anthropic"].key_env, "sk-working")

    result = integrations.apply_provider("openai", key="sk-second",
                                         activate=False)

    assert result.ok, result.error
    assert os.environ["WAKU_PROVIDER"] == "anthropic"
