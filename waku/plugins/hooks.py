"""Running a plugin's hooks (spec 021 C).

Four events, in Claude Code's names and shapes:

    SessionStart       the first turn of a session, before the model runs
    UserPromptSubmit   every turn, before the model runs
    PreToolUse         before each tool call, inside the loop
    Stop               after the reply, on a background thread

Version 1 runs one hook type, `mcp_tool`: a call to a tool on one of the
plugin's own MCP servers, through the bridge Waku already holds. A hook's
`input` is sent with `${name}` replaced by the turn's value, and a name the
turn has no value for becomes "". `command` hooks, which run a program on
this machine, are not run.

What a hook answers is read the way Claude Code reads it:

    hookSpecificOutput.additionalContext   context for this turn's model
    hookSpecificOutput.permissionDecision  "deny" stops a PreToolUse call
    systemMessage                          a line for the person
    waku.capture                           "on", "off" or "paused": whether Waku
                                           Memory is taking this person's turns
                                           (waku-memory spec 086 C2), read only
                                           from a server at Waku Memory's address

A hook that errors, times out or answers `isError` is skipped. The turn goes
on as if the hook were not there, and the trace records the failure.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 10.0
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


CAPTURE_STATES = ("on", "off", "paused")


@dataclass
class HookResult:
    context: list[str] = field(default_factory=list)
    deny: str | None = None
    messages: list[str] = field(default_factory=list)
    # spec 021 amendment E1: Waku Memory's answer to "is capture taking this
    # person's turns", or None when no hook from Waku Memory's address said
    capture: str | None = None


def capture_of(data) -> str | None:
    """`waku.capture` from a hook answer, or None when it holds none."""
    block = data.get("waku") if isinstance(data, dict) else None
    state = block.get("capture") if isinstance(block, dict) else None
    return state if state in CAPTURE_STATES else None


def substitute(value, variables: dict):
    """`value` with every ${name} in its strings replaced. A dict or a list
    is walked; a value that is not a string is sent as JSON text."""
    if isinstance(value, str):
        def one(match: re.Match) -> str:
            found = variables.get(match.group(1), "")
            return found if isinstance(found, str) else json.dumps(found, default=str)
        return _VARIABLE.sub(one, value)
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def read_output(data: dict | None, event: str) -> HookResult:
    result = HookResult()
    if not isinstance(data, dict):
        return result
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
    context = specific.get("additionalContext")
    if isinstance(context, str) and context.strip():
        result.context.append(context)
    if event == "PreToolUse":
        if specific.get("permissionDecision") == "deny":
            result.deny = str(specific.get("permissionDecisionReason") or "a plugin hook refused this call")
        elif data.get("decision") == "block":
            result.deny = str(data.get("reason") or "a plugin hook refused this call")
    message = data.get("systemMessage")
    if isinstance(message, str) and message.strip():
        result.messages.append(message)
    return result


class HookRunner:
    """The hooks of every enabled plugin, run against one bridge."""

    def __init__(self, plugins, bridge, notify: Callable[[str, dict], None] | None = None):
        self.plugins = [p for p in plugins if p.hooks]
        self.bridge = bridge
        self.notify = notify or (lambda kind, ev: None)

    def has(self, event: str) -> bool:
        return any(event in p.hooks for p in self.plugins)

    def _server(self, plugin, name: str) -> str | None:
        """The bridge's id for the server `name` of `plugin`. A hook may name
        it bare or as Claude Code's `plugin:<plugin>:<id>`. When mcp.json
        already had the same address under another id, the bridge connected
        that one, and the hook calls it there."""
        if name.startswith("plugin:"):
            parts = name.split(":", 2)
            if len(parts) != 3 or parts[1] != plugin.name:
                return None
            name = parts[2]
        own = next((s for s in plugin.servers if s["name"] == name), None)
        if own is None:
            return None
        live = getattr(self.bridge, "servers", []) or []
        if any(s.get("name") == name and s.get("plugin") == plugin.name for s in live):
            return name
        url = str(own.get("url", "")).rstrip("/")
        same = next((s for s in live if url and str(s.get("url", "")).rstrip("/") == url
                     and not s.get("plugin")), None)
        return same["name"] if same else None

    def _is_waku_memory(self, server: str) -> bool:
        """Whether the bridge's server `server` is at Waku Memory's address.
        Another server's `waku` field says nothing about Waku Memory."""
        from waku.tools.waku_memory import URL

        live = getattr(self.bridge, "servers", []) or []
        spec = next((s for s in live if s.get("name") == server), None)
        return spec is not None and str(spec.get("url", "")).rstrip("/") == URL

    def run(self, event: str, variables: dict, notify: Callable[[str, dict], None] | None = None) -> HookResult:
        notify = notify or self.notify
        total = HookResult()
        values = {**variables, "hook_event_name": event}
        for plugin in self.plugins:
            for hook in plugin.hooks.get(event, []):
                outcome = self._one(plugin, hook, event, values, notify)
                total.context += outcome.context
                total.messages += outcome.messages
                total.capture = outcome.capture or total.capture
                if outcome.deny and total.deny is None:
                    total.deny = outcome.deny
        return total

    def _one(self, plugin, hook: dict, event: str, values: dict, notify) -> HookResult:
        tool = str(hook.get("tool", ""))
        record = {"event": event, "plugin": plugin.name, "server": str(hook.get("server", "")),
                  "tool": tool, "ok": False, "error": None, "duration_ms": 0}
        started = time.perf_counter()
        try:
            if hook.get("type") != "mcp_tool":
                record["error"] = f"hook type {hook.get('type')!r} is not run by Waku"
                return HookResult()
            server = self._server(plugin, record["server"])
            if server is None or self.bridge is None:
                record["error"] = f"server {record['server']!r} is not connected"
                return HookResult()
            record["server"] = server
            timeout = float(hook.get("timeout") or DEFAULT_TIMEOUT)
            data, failed, text = self.bridge.call_json(
                server, tool, substitute(hook.get("input") or {}, values), timeout)
            if failed:
                record["error"] = text[:200] or "the tool reported an error"
                return HookResult()
            record["ok"] = True
            outcome = read_output(data, event)
            if self._is_waku_memory(server):
                outcome.capture = capture_of(data)
                if outcome.capture:
                    record["capture"] = outcome.capture
            record["context_chars"] = sum(len(c) for c in outcome.context)
            if outcome.deny:
                record["deny"] = outcome.deny[:200]
            return outcome
        except Exception as exc:   # a hook never breaks a turn
            record["error"] = f"{type(exc).__name__}: {exc}"[:200]
            return HookResult()
        finally:
            record["duration_ms"] = int((time.perf_counter() - started) * 1000)
            try:
                notify("hook", record)
            except Exception:
                pass
