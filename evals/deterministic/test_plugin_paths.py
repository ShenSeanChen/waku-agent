"""Review fix 1: a plugin's name never reaches outside <home>/plugins. A
manifest named "../../../victim" once made an install delete a folder beside
<home>; the folder is now named by the catalog, and every copy and delete
checks where it lands."""

from __future__ import annotations

import json

import pytest

from waku.plugins import cli, store


def _market(tmp_path, catalog_name="evil-market", plugin_entry="p", manifest_name="../../../victim"):
    market = tmp_path / "market"
    (market / ".claude-plugin").mkdir(parents=True)
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": catalog_name, "plugins": [{"name": plugin_entry, "source": "./p"}]}))
    (market / "p" / ".waku-plugin").mkdir(parents=True)
    (market / "p" / ".waku-plugin" / "plugin.json").write_text(json.dumps({"name": manifest_name}))
    return market


def test_a_manifest_name_with_dots_cannot_delete_a_folder_beside_home(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("x")
    home = tmp_path / "home"
    cli.run(["marketplace", "add", str(_market(tmp_path))], home, lambda s: False)
    (store.folder(home) / "evil-market").mkdir(parents=True)
    reply = cli.run(["install", "p@evil-market"], home, lambda s: True)
    assert "Installed p from evil-market" in reply
    assert (victim / "precious.txt").read_text() == "x"
    assert (store.folder(home) / "evil-market" / "p" / ".waku-plugin").is_dir()
    assert store.read(home)["p@evil-market"]["path"] == "evil-market/p"


@pytest.mark.parametrize("target", ["..@evil-market", "p@..", ".hidden@m", "a/b@m", "p@m/../x"])
def test_names_that_are_not_folder_names_are_refused(tmp_path, target):
    home = tmp_path / "home"
    assert "is not a plugin name Waku installs" in cli.run(["install", target], home, lambda s: True)


def test_remove_refuses_a_recorded_path_outside_the_plugins_folder(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    home = tmp_path / "home"
    store.write(home, {"p@m": {"name": "p", "marketplace": "m", "path": "../../victim", "enabled": True}})
    assert "is not a folder inside" in cli.run(["remove", "p@m"], home, lambda s: True)
    assert victim.is_dir()
    assert store.enabled(home) == []


def test_valid_names():
    assert store.valid_name("waku-memory") and store.valid_name("waku_1.2")
    assert not any(store.valid_name(n) for n in ("", ".", "..", ".x", "a/b", "../v", "a b"))
