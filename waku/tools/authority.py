"""The authority ladder, enforced at the tool gate.

A CTO does not execute everything indiscriminately; neither should the harness.
Each rung maps to a required authority level; a tool runs only when the current
level is high enough, and `irreversible` tools (named in a project's
project.toml) never run without the owner.

Enforced in ToolRegistry.execute, not stated in a prompt: the model sees an
honest refusal it cannot override. See docs/project-harness.md §5.
"""

from __future__ import annotations

from pathlib import Path

# Ordered least → most authority.
LEVELS = ["observe", "recommend", "plan", "sandbox", "reversible", "irreversible"]

# Required level per core tool. Unlisted tools default to "recommend".
TOOL_LEVELS = {
    "search_web": "observe",
    "list_events": "observe",
    "browse_web": "observe",
    "create_event": "plan",
    "save_note": "plan",
    "send_message": "plan",     # drafts to the outbox; a real send is a later concern
    "manage_memory": "plan",
    "update_soul": "plan",
    "create_skill": "plan",
    "delegate_task": "sandbox",
    "run_command": "sandbox",
}


class AuthorityGate:
    """Decides, per tool, whether the current authority level permits it."""

    def __init__(self, level: str = "recommend",
                 irreversible: tuple | list | set | frozenset = ()):
        self.level = level if level in LEVELS else "recommend"
        self.irreversible = frozenset(irreversible)

    def required(self, tool: str) -> str:
        return TOOL_LEVELS.get(tool, "recommend")

    def allow(self, tool: str) -> tuple[bool, str]:
        """Return (allowed, why_not); why_not is '' when allowed."""
        if tool in self.irreversible:
            return False, f"'{tool}' requires owner approval (irreversible)"
        if LEVELS.index(self.required(tool)) > LEVELS.index(self.level):
            return False, (f"'{tool}' needs '{self.required(tool)}' authority, "
                           f"current level is '{self.level}'")
        return True, ""


def gate_from_project(project_dir: Path | str) -> AuthorityGate | None:
    """Build the gate from a harness's project.toml [authority] table. None when
    there is no project.toml (or no [authority]) — the caller runs ungated."""
    import tomllib

    path = Path(project_dir) / "project.toml"
    if not path.is_file():
        return None
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    auth = cfg.get("authority")
    if not auth:
        return None
    return AuthorityGate(level=auth.get("default", "recommend"),
                         irreversible=auth.get("irreversible", []))


def authority_to_sandbox(authority: str) -> str | None:
    """The coding-agent sandbox a role's rung maps to. None = owner-gated: the
    caller must refuse rather than auto-run.

    The thinking rungs (observe/recommend/plan) get read-only — they produce
    text, not files; `sandbox` gets workspace-write (the engineer); the top two
    rungs never auto-run."""
    if authority in ("observe", "recommend", "plan"):
        return "read-only"
    if authority == "sandbox":
        return "workspace-write"
    return None   # reversible / irreversible
