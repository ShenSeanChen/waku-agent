#!/usr/bin/env python3
"""Jev as an MCP tool, so agents whose loop you do not own can still call it.

Jev ships no MCP server. This is one: stdio JSON-RPC, stdlib only, one tool.

    codex mcp add jev -- python3 lab/jev-system-one/integrations/jev_mcp.py

Running locally covers every agent on your machine. Grok Bot and Muse run on
someone else's cloud and can only reach a public URL, so serving them means
hosting this behind HTTPS with the caller's own TypeSafe key -- we never spend
ours on theirs.

Note what this door cannot do: the agent decides whether to call it. A tool can
be ignored. Use the hook when the check has to be unavoidable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev import ask  # noqa: E402

TOOL = {
    "name": "judge",
    "description": ("Answer typed questions about a block of text and return probabilities "
                    "instead of prose. Use for classifying, scoring, ranking or checking many "
                    "items cheaply. Not for writing anything."),
    "inputSchema": {
        "type": "object",
        "required": ["state", "questions"],
        "properties": {
            "state": {"type": "string", "description": "The text to judge."},
            "questions": {"type": "object", "description":
                "Map of your own question id to {type: noul|choice|score, instructions, "
                "criteria}. criteria is a dict of key->description for choice, and an "
                "ordered list of level descriptions for score."},
        },
    },
}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "jev", "version": "0.1.0"}}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        args = msg["params"]["arguments"]
        answers = ask(args["state"], args["questions"])
        answers.pop("_cost", None)
        return {"content": [{"type": "text", "text": json.dumps(answers)}]}
    if mid is None:
        return None                                   # a notification; nothing to answer
    raise LookupError(method)


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        try:
            result = handle(msg)
        except LookupError as exc:
            out = {"jsonrpc": "2.0", "id": msg.get("id"),
                   "error": {"code": -32601, "message": f"unknown method: {exc}"}}
        except Exception as exc:                       # noqa: BLE001 - report, never die
            out = {"jsonrpc": "2.0", "id": msg.get("id"),
                   "error": {"code": -32000, "message": str(exc)}}
        else:
            if result is None:
                continue
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
