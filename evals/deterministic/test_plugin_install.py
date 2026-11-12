"""Spec 021 B: marketplaces and install. Offline: a marketplace is a folder
here, or a GitHub archive built in memory and handed to the downloader."""

from __future__ import annotations

import io
import json
import tarfile

from evals.deterministic.plugin_fixtures import marketplace as make_marketplace
from waku.plugins import cli, marketplace, store


def _yes(shown):
    return True


def _no(shown):
    return False


def _added(tmp_path):
    home = tmp_path / "home"
    market = make_marketplace(tmp_path / "market")
    assert cli.run(["marketplace", "add", str(market)], home, _no).startswith(
        "Added marketplace waku-memory")
    return home


def test_marketplace_add_records_the_marketplace_under_its_own_name(tmp_path):
    home = _added(tmp_path)
    assert marketplace.read(home) == {"waku-memory": {"path": str((tmp_path / "market").resolve())}}


def test_install_shows_what_the_plugin_brings_and_writes_nothing_when_declined(tmp_path):
    home = _added(tmp_path)
    seen = []
    reply = cli.run(["install", "waku@waku-memory"], home, lambda shown: seen.append(shown) or False)
    assert reply.endswith("Nothing was installed.")
    shown = seen[0]
    assert "waku_memory  https://api.waku.one/mcp  (signs in in your browser)" in shown
    assert "SessionStart: waku_memory session.hook" in shown and "Stop: waku_memory session.hook" in shown
    assert "Skills: waku" in shown
    assert store.read(home) == {} and not (store.folder(home) / "waku-memory").exists()


def test_install_copies_only_the_named_plugin_and_records_it(tmp_path):
    home = _added(tmp_path)
    reply = cli.run(["install", "waku@waku-memory"], home, _yes)
    assert "Installed waku from waku-memory" in reply
    assert "Sign in to waku_memory: waku mcp login waku_memory" in reply
    assert (store.folder(home) / "waku-memory" / "waku" / ".waku-plugin" / "plugin.json").exists()
    assert not (store.folder(home) / "waku-memory" / "docs").exists()
    assert store.read(home)["waku@waku-memory"] == {
        "name": "waku", "marketplace": "waku-memory", "path": "waku-memory/waku",
        "ref": "local", "version": "0.15.0", "enabled": True}
    assert [p.name for p in store.enabled(home)] == ["waku"]


def test_a_source_outside_the_marketplace_is_refused(tmp_path):
    home = _added(tmp_path)
    (tmp_path / "outside" / ".waku-plugin").mkdir(parents=True)
    (tmp_path / "outside" / ".waku-plugin" / "plugin.json").write_text('{"name": "escape"}')
    reply = cli.run(["install", "escape@waku-memory"], home, _yes)
    assert "points outside the marketplace" in reply and store.read(home) == {}


def test_a_claude_code_plugin_installs_and_says_its_hooks_are_skipped(tmp_path):
    home = _added(tmp_path)
    reply = cli.run(["install", "docs@waku-memory"], home, _yes)
    assert "Hooks: skipped" in reply
    assert "runs a program on this machine: npx -y docs-server" in reply
    assert "hooks skipped" in cli.run(["list"], home, _no)


def test_disable_enable_and_remove(tmp_path):
    home = _added(tmp_path)
    cli.run(["install", "waku@waku-memory"], home, _yes)
    (home / "mcp-auth").mkdir()
    (home / "mcp-auth" / "waku_memory.json").write_text("{}")
    cli.run(["disable", "waku"], home, _no)
    assert store.enabled(home) == [] and "disabled" in cli.run(["list"], home, _no)
    cli.run(["enable", "waku@waku-memory"], home, _no)
    assert [p.name for p in store.enabled(home)] == ["waku"]
    cli.run(["remove", "waku"], home, _no)
    assert store.read(home) == {} and not (store.folder(home) / "waku-memory" / "waku").exists()
    assert (home / "mcp-auth" / "waku_memory.json").exists()   # the sign-in is kept


def test_update_copies_the_marketplace_again(tmp_path):
    home = _added(tmp_path)
    cli.run(["install", "waku@waku-memory"], home, _yes)
    source = tmp_path / "market" / "plugins" / "waku" / ".waku-plugin" / "plugin.json"
    data = json.loads(source.read_text())
    source.write_text(json.dumps({**data, "version": "0.16.0"}))
    assert "Updated waku@waku-memory to 0.16.0" in cli.run(["update", "waku"], home, _no)
    assert store.read(home)["waku@waku-memory"]["version"] == "0.16.0"


def test_list_names_a_plugin_server_mcp_json_already_takes(tmp_path):
    home = _added(tmp_path)
    cli.run(["install", "waku@waku-memory"], home, _yes)
    (home / "mcp.json").write_text(json.dumps({"servers": [
        {"name": "waku_memory", "url": "https://elsewhere/mcp", "auth_env": "K"}]}))
    assert "server waku_memory (skipped: mcp.json names this id)" in cli.run(["list"], home, _no)


def _archive(files: dict, links: tuple = ()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buffer.getvalue()


def test_a_github_marketplace_is_one_archive_and_nothing_escapes_it(tmp_path, monkeypatch):
    plugin = {"name": "waku", "version": "1", "mcpServers": {"waku_memory": {"url": "https://api.waku.one/mcp"}}}
    files = {
        "repo-main/.claude-plugin/marketplace.json": json.dumps(
            {"name": "waku-memory", "plugins": [{"name": "waku", "source": "./plugins/waku"}]}),
        "repo-main/plugins/waku/.waku-plugin/plugin.json": json.dumps(plugin),
        "repo-main/../../escaped.txt": "out",
    }
    data = _archive(files, links=(("repo-main/plugins/waku/link", "/etc/passwd"),))
    urls = []
    monkeypatch.setattr(marketplace, "_download", lambda url: urls.append(url) or data)
    home = tmp_path / "home"
    assert cli.run(["marketplace", "add", "waku-one/waku-memory-plugins"], home, _no).startswith(
        "Added marketplace waku-memory")
    assert "Installed waku" in cli.run(["install", "waku@waku-memory"], home, _yes)
    assert urls[-1] == "https://codeload.github.com/waku-one/waku-memory-plugins/tar.gz/main"
    installed = store.folder(home) / "waku-memory" / "waku"
    assert not (installed / "link").exists() and not (tmp_path / "escaped.txt").exists()
    assert store.read(home)["waku@waku-memory"]["ref"] == "main"


def test_the_waku_memory_marketplace_is_known_before_any_add(tmp_path, monkeypatch):
    monkeypatch.setattr(marketplace, "_download", lambda url: (_ for _ in ()).throw(OSError("offline")))
    reply = cli.run(["install", "waku@waku-memory"], tmp_path, _yes)
    assert "codeload.github.com/waku-one/waku-memory-plugins" in reply


def test_hosted_refuses_every_plugin_command(tmp_path):
    home = _added(tmp_path)
    assert cli.run(["install", "waku@waku-memory"], home, _yes, hosted=True) == cli.HOSTED_REFUSAL
    assert cli.chat("install waku@waku-memory --yes", home, hosted=True) == cli.HOSTED_REFUSAL
    assert store.read(home) == {}


def test_the_dashboard_chat_previews_until_yes(tmp_path):
    home = _added(tmp_path)
    first = cli.chat("install waku@waku-memory", home, hosted=False)
    assert first.endswith("To install it, send: /plugin install waku@waku-memory --yes")
    assert store.read(home) == {}
    assert "Installed waku" in cli.chat("install waku@waku-memory --yes", home, hosted=False)
    assert "waku@waku-memory" in store.read(home)


def test_extract_writes_nothing_outside_its_folder_and_no_links(tmp_path):
    dest = tmp_path / "x" / "y"
    dest.mkdir(parents=True)
    data = _archive({"repo-main/a.txt": "a", "repo-main/../../escaped.txt": "out"},
                    links=(("repo-main/link", "/etc/passwd"),))
    top = marketplace._extract(data, dest)
    assert (top / "a.txt").read_text() == "a"
    assert not (tmp_path / "x" / "escaped.txt").exists() and not (top / "link").exists()
