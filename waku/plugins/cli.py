"""`waku plugin ...` in the terminal and `/plugin ...` in the dashboard chat
(spec 021 B2, B3). Both call `run()`, which returns what to print.

  waku plugin marketplace add <owner/repo[@ref] | folder>
  waku plugin marketplace list | remove <name>
  waku plugin install <plugin>@<marketplace>
  waku plugin list
  waku plugin update | remove | enable | disable <plugin>[@<marketplace>]

Before an install copies anything, it prints what the plugin brings: each
MCP server's address, each hook's event and tool, and each skill. The
terminal then asks. The dashboard chat prints the same preview, and the
person sends the command again with `--yes`. A plugin loads when Waku next
starts.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from waku.plugins import manifest, marketplace, store
from waku.plugins.marketplace import MarketplaceError

HOSTED_REFUSAL = ("Plugins are not available on hosted Waku. A hosted container's servers "
                  "and memory are set up when you sign in.")

USAGE = __doc__.split("\n\n")[1]


def preview(plugin: manifest.Plugin, market: str) -> str:
    """What the plugin brings, for the person to read before it is copied."""
    lines = [f"{plugin.name} {plugin.version}".strip() + f" from {market}"]
    if plugin.description:
        lines.append(plugin.description)
    if plugin.servers:
        lines.append("MCP servers Waku connects to when it starts:")
        for server in plugin.servers:
            if server.get("url"):
                how = ("signs in in your browser" if server.get("oauth")
                       else "uses the key in $" + server["auth_env"] if server.get("auth_env")
                       else "sends the plugin's own header")
                lines.append(f"  {server['name']}  {server['url']}  ({how})")
            else:
                program = " ".join([server.get("command", ""), *server.get("args", [])])
                lines.append(f"  {server['name']}  runs a program on this machine: {program}")
    if plugin.hooks:
        lines.append("Hooks, each a call to one of the servers above:")
        for event, hooks in plugin.hooks.items():
            for hook in hooks:
                lines.append(f"  {event}: {hook.get('server', '?')} {hook.get('tool', '?')}"
                             + ("" if hook.get("type") == "mcp_tool" else
                                f" (type {hook.get('type')!r}, which Waku does not run)"))
    elif plugin.hooks_skipped:
        lines.append("Hooks: skipped. This plugin has no .waku-plugin manifest, and its hooks "
                     "are written for Claude Code.")
    skills = sorted(p.parent.name for p in plugin.skills_dir.rglob("SKILL.md")) if plugin.skills_dir else []
    if skills:
        lines.append("Skills: " + ", ".join(skills))
    return "\n".join(lines)


def _key(home: Path, given: str) -> str:
    """`waku` or `waku@waku-memory`, as the key installed.json uses."""
    records = store.read(home)
    if given in records:
        return given
    matches = [k for k in records if k.split("@", 1)[0] == given]
    if len(matches) == 1:
        return matches[0]
    raise MarketplaceError(f"no installed plugin '{given}'" if not matches
                           else f"'{given}' is installed from more than one marketplace: "
                                + ", ".join(matches))


def _copy(source: Path, home: Path, relative: str) -> None:
    dest = store.inside(home, relative)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "node_modules"))


def migrate_mcp_json(home: Path, plugin: manifest.Plugin) -> list[str]:
    """Spec 021 D3: an mcp.json entry the plugin now provides is removed. It
    must have the plugin server's id and address and sign in with OAuth, so
    its token file, <home>/mcp-auth/<id>.json, is the plugin server's too,
    and nobody signs in again. An entry with `auth_env` (hosted provisioning
    writes that shape) or anything else that differs is left as it is."""
    config = home / "mcp.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return []
    provided = {(s["name"], str(s.get("url", "")).rstrip("/")) for s in plugin.servers
                if s.get("url") and s.get("oauth")}
    moved = [s for s in servers if isinstance(s, dict) and s.get("oauth") and not s.get("auth_env")
             and (s.get("name"), str(s.get("url", "")).rstrip("/")) in provided]
    if not moved:
        return []
    data["servers"] = [s for s in servers if s not in moved]
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return [s["name"] for s in moved]


def install(home: Path, target: str, confirm: Callable[[str], bool]) -> str:
    name, _, market = target.partition("@")
    if not name or not market:
        return "Name the plugin and its marketplace: waku plugin install <plugin>@<marketplace>"
    if not (store.valid_name(name) and store.valid_name(market)):
        return (f"'{target}' is not a plugin name Waku installs. A name holds letters, "
                "digits, '.', '_' and '-', and does not start with a dot.")
    entry = marketplace.lookup(home, market)
    if entry is None:
        return (f"No marketplace named '{market}'. Add it first: "
                "waku plugin marketplace add <owner/repo>")
    with marketplace.fetch(entry) as root:
        catalog = marketplace.catalog(root)
        source = marketplace.plugin_folder(root, catalog, name)
        plugin = manifest.load(source)
        if plugin is None:
            return f"'{name}' in '{market}' has no plugin manifest."
        shown = preview(plugin, market)
        if not confirm(shown):
            return shown + "\n\nNothing was installed."
        # the catalog's name, checked above, never the manifest's: the
        # manifest is the plugin's own file and may name anything
        relative = f"{market}/{name}"
        _copy(source, home, relative)
    if market not in marketplace.read(home):
        marketplace.write(home, {**marketplace.read(home), market: entry})
    records = store.read(home)
    records[f"{name}@{market}"] = {
        "name": name, "marketplace": market, "path": relative,
        "ref": entry.get("ref") or "local", "version": plugin.version, "enabled": True}
    store.write(home, records)
    moved = migrate_mcp_json(home, plugin)
    lines = [shown, "", f"Installed {name} from {market}. It loads when Waku next starts."]
    if moved:
        lines.append("Moved " + ", ".join(moved) + " from mcp.json to the plugin. "
                     "Your sign-in stays as it is.")
    from waku.tools.mcp_cli import _auth_file

    for server in plugin.servers:
        if server.get("oauth") and not _auth_file(home, server["name"]).exists():
            lines.append(f"Sign in to {server['name']}: waku mcp login {server['name']}")
    return "\n".join(lines)


def update(home: Path, given: str) -> str:
    key = _key(home, given)
    record = store.read(home)[key]
    entry = marketplace.lookup(home, record["marketplace"])
    if entry is None:
        return f"The marketplace '{record['marketplace']}' is no longer recorded."
    with marketplace.fetch(entry) as root:
        source = marketplace.plugin_folder(root, marketplace.catalog(root), record["name"])
        plugin = manifest.load(source)
        if plugin is None:
            return f"'{record['name']}' no longer has a plugin manifest; nothing was changed."
        _copy(source, home, record["path"])
    records = store.read(home)
    records[key].update(version=plugin.version, ref=entry.get("ref") or "local")
    store.write(home, records)
    return f"Updated {key} to {plugin.version or 'the latest version'}. It loads when Waku next starts."


def listing(home: Path) -> str:
    records = store.read(home)
    if not records:
        return "No plugins installed. Try: waku plugin install waku@waku-memory"
    from waku.tools.mcp_client import merge

    try:
        config = json.loads((home / "mcp.json").read_text(encoding="utf-8")).get("servers", [])
    except (OSError, ValueError):
        config = []
    _, skipped = merge(config, store.servers(home))
    lines = []
    for key, record in records.items():
        try:
            plugin = manifest.load(store.inside(home, record.get("path", "")))
        except store.UnsafePath:
            plugin = None
        state = "enabled" if record.get("enabled", True) else "disabled"
        if plugin is None:
            lines.append(f"{key}  {state}  (its folder is missing)")
            continue
        lines.append(f"{key} {plugin.version}  {state}".rstrip())
        for server in plugin.servers:
            note = " (skipped: mcp.json names this id)" if (plugin.name, server["name"]) in skipped else ""
            lines.append(f"  server {server['name']}{note}")
        if plugin.hooks:
            lines.append("  hooks " + ", ".join(plugin.hooks))
        elif plugin.hooks_skipped:
            lines.append("  hooks skipped (a Claude Code plugin, with no .waku-plugin manifest)")
    return "\n".join(lines)


def _set_enabled(home: Path, given: str, enabled: bool) -> str:
    key = _key(home, given)
    records = store.read(home)
    records[key]["enabled"] = enabled
    store.write(home, records)
    return f"{'Enabled' if enabled else 'Disabled'} {key}. This takes effect when Waku next starts."


def remove(home: Path, given: str) -> str:
    key = _key(home, given)
    records = store.read(home)
    record = records.pop(key)
    folder = store.inside(home, record.get("path", ""))
    if folder.is_dir():
        shutil.rmtree(folder)
    store.write(home, records)
    return f"Removed {key}. Sign-ins in mcp-auth are kept; waku mcp login can replace one."


def _marketplace(home: Path, args: list[str]) -> str:
    action = args[0] if args else "list"
    if action == "add" and len(args) == 2:
        entry = marketplace.entry_for(args[1])
        with marketplace.fetch(entry) as root:
            catalog = marketplace.catalog(root)
        names = [p.get("name") for p in catalog.get("plugins") or [] if isinstance(p, dict)]
        marketplace.write(home, {**marketplace.read(home), catalog["name"]: entry})
        return f"Added marketplace {catalog['name']}. Its plugins: {', '.join(names) or 'none'}."
    if action == "remove" and len(args) == 2:
        entries = marketplace.read(home)
        if entries.pop(args[1], None) is None:
            return f"No marketplace named '{args[1]}'."
        marketplace.write(home, entries)
        return f"Removed marketplace {args[1]}. Plugins installed from it stay installed."
    if action == "list":
        entries = {**marketplace.KNOWN, **marketplace.read(home)}
        return "\n".join(f"{name}  {e.get('repo') or e.get('path')}" for name, e in entries.items())
    return USAGE


def run(args: list[str], home: Path, confirm: Callable[[str], bool], hosted: bool = False) -> str:
    if hosted:
        return HOSTED_REFUSAL
    if not args:
        return USAGE
    command, rest = args[0], args[1:]
    try:
        if command == "marketplace":
            return _marketplace(home, rest)
        if command == "install" and len(rest) == 1:
            return install(home, rest[0], confirm)
        if command == "list":
            return listing(home)
        if command in ("update", "remove", "enable", "disable") and len(rest) == 1:
            return {"update": update, "remove": remove,
                    "enable": lambda h, g: _set_enabled(h, g, True),
                    "disable": lambda h, g: _set_enabled(h, g, False)}[command](home, rest[0])
    except (MarketplaceError, store.UnsafePath) as exc:
        return str(exc)
    return USAGE


def chat(arg: str, home: Path, hosted: bool) -> str:
    """`/plugin <arg>` from the dashboard chat. An install previews until
    the command is sent again with --yes."""
    words = arg.split()
    yes = "--yes" in words
    words = [w for w in words if w != "--yes"]

    def confirm(shown: str) -> bool:
        return yes

    reply = run(words, home, confirm, hosted=hosted)
    if words[:1] == ["install"] and not yes and reply.endswith("Nothing was installed."):
        reply = reply.removesuffix("Nothing was installed.") + (
            f"To install it, send: /plugin {' '.join(words)} --yes")
    return reply


def cli_main(args: list[str]) -> int:
    from waku.config import load_settings

    home = load_settings().home

    def confirm(shown: str) -> bool:
        print(shown)
        if not sys.stdin.isatty():
            print("Not a terminal, so nothing is installed. Run this in a terminal to answer.")
            return False
        return input("\nInstall it? [y/N] ").strip().lower() in ("y", "yes")

    reply = run(args, home, confirm)
    # the terminal already showed the preview; print only what came after it
    if args[:1] == ["install"] and "\n\n" in reply:
        reply = reply.split("\n\n", 1)[1]
    print(reply)
    return 0
