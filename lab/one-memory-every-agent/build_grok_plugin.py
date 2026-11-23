"""Build a Waku plugin folder in the format of xAI's plugin marketplace.

xAI's plugin marketplace (github.com/xai-org/plugin-marketplace) serves Grok
Build, xAI's coding agent, not Grok Bot; Grok Bot takes a custom MCP connector
in its own settings instead. The marketplace takes a folder holding
skills/*/SKILL.md and a .mcp.json that names remote MCP servers. Waku's skills
are already SKILL.md folders, so the plugin is Waku's own skills plus Waku
Memory's MCP server:

    python lab/one-memory-every-agent/build_grok_plugin.py
    -> lab/one-memory-every-agent/grok-plugin/   (ignored by git)

Only the skills that ship with Waku go in: community skills belong to their
authors, and the skills in your own WAKU_HOME are yours. The .mcp.json carries
${WAKU_MEMORY_API_KEY} in place of a key and never a real one. Whether Grok
Build expands that variable is still untested.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for waku/

from waku.memory.procedural.exporter import _skills  # noqa: E402
from waku.tools.waku_memory import URL  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "grok-plugin"


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "skills").mkdir(parents=True)

    # A home with no skills folder, so only the skills Waku ships are picked up.
    shipped = [s for s in _skills(HERE / "_no_home") if s.path.parent.parent.name != "community"]
    for skill in shipped:
        shutil.copytree(skill.path.parent, OUT / "skills" / skill.name,
                        ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))

    mcp = {"mcpServers": {"waku-memory": {
        "type": "http",
        "url": URL,
        "headers": {"Authorization": "Bearer ${WAKU_MEMORY_API_KEY}"},
    }}}
    (OUT / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
    plugin = {
        "name": "waku",
        "description": "One memory across your agents: Waku Memory over MCP, plus Waku's skills.",
        "version": "0.1.0",
        "homepage": "https://www.waku.one",
        "author": {"name": "AutoManus Technologies, Inc."},
    }
    (OUT / "plugin.json").write_text(json.dumps(plugin, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {OUT}")
    for skill in shipped:
        print(f"  skills/{skill.name}/")
    print("  .mcp.json  (Waku Memory, key from $WAKU_MEMORY_API_KEY)")
    print("  plugin.json")
    entry = {"name": "waku", "source": {"type": "local", "path": "./external_plugins/waku"},
             "description": plugin["description"]}
    print("\nThe marketplace catalog entry, if this is submitted:")
    print(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
