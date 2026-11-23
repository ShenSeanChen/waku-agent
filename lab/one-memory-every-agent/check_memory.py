"""Save one fact to Waku Memory from here, then ask your other agents for it.

The Waku agent saves a fact with a unique code word through its own MCP
connection, searches it back, and prints the question to ask Claude Code,
Codex, Grok Bot and Muse. If each of them finds the same fact, one memory
reaches every agent.

    pip install 'waku-agent[mcp]' && waku connect waku-memory      # once
    uv run python lab/one-memory-every-agent/check_memory.py              # save + search
    uv run python lab/one-memory-every-agent/check_memory.py --forget ID  # remove it afterwards

It writes to the Waku Memory account you connected, in the scope
"lab:one-memory-every-agent", so the check never mixes with your real memories.
"""

from __future__ import annotations

import sys

# A bare `python` on macOS is the system 3.9, which cannot import Waku at all.
# Say so and name the command, rather than failing on the first import.
if sys.version_info < (3, 11):
    sys.exit("This needs Waku's own Python (3.11+). Run it with:\n"
             "  uv run python lab/one-memory-every-agent/check_memory.py")

import argparse  # noqa: E402
import json  # noqa: E402
import secrets  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for waku/

from waku.config import load_settings  # noqa: E402
from waku.tools.waku_memory import URL  # noqa: E402

SCOPE = "lab:one-memory-every-agent"


def _server(home: Path) -> str | None:
    """The name mcp.json gives Waku Memory's server, or None if it is not there."""
    config = home / "mcp.json"
    if not config.exists():
        return None
    servers = json.loads(config.read_text(encoding="utf-8")).get("servers", [])
    return next((s["name"] for s in servers if s.get("url", "").rstrip("/") == URL), None)


def _json(reply: str) -> dict | None:
    try:
        return json.loads(reply)
    except json.JSONDecodeError:
        print(reply)   # the bridge reports a failed call as plain text
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--forget", metavar="ID", help="soft-delete a check fact saved earlier")
    args = parser.parse_args()

    home = load_settings().home
    name = _server(home)
    if name is None:
        print("Waku Memory is not connected here. Run: waku connect waku-memory")
        return 1

    from waku.tools.mcp_client import MCPBridge

    bridge = MCPBridge(home / "mcp.json")
    try:
        bridge.start()
        if args.forget:
            print(bridge.call(name, "memory.forget", {"id": args.forget}))
            return 0

        word = f"wakucheck{secrets.token_hex(3)}"
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        body = f"Lab check {word}: the Waku agent saved this fact on {day}."
        saved = _json(bridge.call(name, "memory.remember", {"body": body, "scope": SCOPE}))
        if saved is None:
            return 1
        memory_id = saved.get("memory", {}).get("id", "?")
        print(f"saved   {memory_id}  \"{body}\"")

        found = _json(bridge.call(name, "memory.search", {"query": word, "scope": SCOPE, "limit": 3}))
        if found is None:
            return 1
        hits = next((v for v in found.values() if isinstance(v, list)), [])
        print(f"search  {len(hits)} result(s) for {word} from the Waku agent")

        print("\nNow ask each of the other agents the same question:")
        print(f'  "Search Waku Memory for {word} and quote what it says."')
        print("  Claude Code · Codex · Grok Bot · Muse")
        print("\nRecord each answer in the What we found table of this topic's README.")
        print(f"Afterwards: uv run python lab/one-memory-every-agent/check_memory.py --forget {memory_id}")
        return 0 if hits else 1
    finally:
        bridge.close()


if __name__ == "__main__":
    sys.exit(main())
