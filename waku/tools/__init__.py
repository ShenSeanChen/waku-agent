"""The agent's tools. Flagship-task tools (calendar/notes/messages), memory
self-management (manage_memory/update_soul/create_skill), and opt-in adapters:
Apple ecosystem (WAKU_APPLE_TOOLS=1) and MCP servers (.waku/mcp.json)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from waku.config import Settings
from waku.tools import (
    calendar,
    connectors,
    memory_admin,
    messages,
    notes,
    schedule,
    search,
    waiting,
)
from waku.tools.registry import ToolRegistry


def build_registry(conn: sqlite3.Connection, settings: Settings, memory=None,
                   session_id: Callable[[], str] | None = None) -> ToolRegistry:
    """`session_id` says which conversation the turn in flight belongs to.
    schedule_task stores it, so a timer wakes the agent in that conversation."""
    registry = ToolRegistry()
    registry.register(
        calendar.make_tool(
            conn,
            settings.home,
            apple_calendar=settings.apple_calendar,
            google_calendar=settings.google_calendar,
            google_calendar_id=settings.google_calendar_id,
        )
    )
    # Read side: "what's on my calendar?" — one tool across every connected
    # source (Google when signed in, plus waku's own), so the model never has
    # to guess which calendar the user meant.
    registry.register(calendar.make_list_tool(conn, settings.home))
    registry.register(notes.make_tool(conn, memory))
    registry.register(messages.make_tool(settings.home))
    # A follow-up the agent sets for itself (spec 023): a row in `timers`
    # that `waku serve` turns into an event when its time comes.
    registry.register(schedule.make_tool(settings.home, session_id=session_id))
    # What waits, and cancelling one of them (spec 024): "what reminders do I
    # have?" and "cancel the Thursday one" work from chat.
    for tool in waiting.make_tools(settings.home, session_id=session_id):
        registry.register(tool)
    # Connecting a server of the person's own from chat (spec 025). Both
    # take the owner's yes first, whatever the environment allows.
    for tool in connectors.make_tools(settings.home):
        registry.register(tool)
    # Web search — pairs with create_event for the multi-tool loop demo
    # ("find the World Cup games left and add them to my calendar").
    registry.register(search.make_tool())

    # Memory self-management — the agent can correct/forget memory, learn rules,
    # and author its own skills (feels like a personal agent, not a black box).
    if memory is not None:
        registry.register(memory_admin.make_manage_memory_tool(memory))
        registry.register(memory_admin.make_update_soul_tool(settings))
        registry.register(memory_admin.make_create_skill_tool(settings, memory))

    # Experimental tools — off by default; opt in with WAKU_EXPERIMENTAL=1.
    # delegate_task (sub-agents via pi) is live; terminal and browser are
    # still skeletons that report "coming soon".
    #
    # Trust settings.experimental ALONE. load_settings() already defaults it from
    # WAKU_EXPERIMENTAL, so re-checking the env here would let the global switch
    # override an explicit False — and the arena passes experimental=False for
    # every non-coding race. Once the dashboard could write WAKU_EXPERIMENTAL=1,
    # that OR silently forced delegate_task into races that never asked for it.
    if getattr(settings, "experimental", False):
        from waku.tools import experimental

        for t in experimental.make_tools(settings):
            registry.register(t)

    # Apple ecosystem readers/writers (opt-in; first use triggers macOS prompts).
    if settings.apple_tools:
        from waku.tools import apple

        for t in apple.make_tools():
            registry.register(t)

    # Read-only GitHub via the gh CLI (opt-in; uses gh's own auth, no token here).
    if getattr(settings, "gh_tool", False):
        from waku.tools import github

        registry.register(github.make_tool(default_repo=getattr(settings, "gh_repo", "")))

    # MCP servers (opt-in via .waku/mcp.json, or an installed plugin's own,
    # spec 021). Either one is enough to start the bridge.
    from waku.plugins import store

    mcp_config = settings.home / "mcp.json"
    plugin_servers = store.servers(settings.home)
    if mcp_config.exists() or plugin_servers:
        try:
            from waku.tools.mcp_client import MCPBridge
            from waku.tools.waku_memory import is_waku_memory

            # Spec 020: "Keep local only" leaves Waku Memory unconnected even
            # when mcp.json or a plugin names it, and touches no other server.
            memory_off = getattr(settings, "memory_mode", "") == "off"
            # Spec 025: the always-on agent runs where nobody may be looking,
            # so a server that still needs its sign-in is left out there and
            # no browser opens. The owner signs in through the link.
            wait_for_sign_in = getattr(settings, "mcp_sign_in", True)

            def left_out(spec: dict) -> bool:
                return ((memory_off and is_waku_memory(spec))
                        or (not wait_for_sign_in
                            and connectors.needs_sign_in(settings.home, spec)))

            # None when neither rule applies: every server is connected.
            skip = left_out if memory_off or not wait_for_sign_in else None
            bridge = MCPBridge(mcp_config, skip=skip, extra_servers=plugin_servers)
            for t in bridge.start():
                registry.register(t)
            registry.mcp_bridge = bridge  # so Waku.close() can stop the servers
        except ImportError:
            print("mcp.json found but the 'mcp' package is missing — pip install 'waku-agent[mcp]'")

    return registry
