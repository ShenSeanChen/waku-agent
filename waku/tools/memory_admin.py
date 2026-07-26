"""Tools that let the agent manage its OWN memory — so it feels like a personal
assistant that learns, not a black box. Three tools:

  manage_memory  — search / update / delete facts and episodes (the CRUD)
  update_soul    — append a durable behaviour rule to SOUL.md (its persona)
  create_skill   — write a new SKILL.md, so the agent builds its own procedures

Everything writes to the same local files the dashboard shows; nothing leaves
the machine. update_soul is append-only (the agent can't delete its own honesty
rules); a human does full rewrites in the dashboard.
"""

from __future__ import annotations

import re

from waku.memory import REPO_SKILLS
from waku.memory.procedural.loader import _parse_text
from waku.tools.registry import Tool

SOUL_MAX = 8000
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def make_manage_memory_tool(memory) -> Tool:
    facts = memory.facts
    episodes = memory.episodes

    def manage_memory(action: str, kind: str = "fact", id: int = 0,
                      query: str = "", content: str = "", subject: str = "") -> str:
        action = (action or "").lower()
        if action == "search":
            if kind == "episode":
                rows = episodes.list(20)
                if query:
                    rows = [r for r in rows if query.lower() in r["summary"].lower()]
                return "\n".join(f"#{r['id']} ({r['happened_at']}) {r['summary']}" for r in rows[:8]) or "no episodes"
            rows = facts.search_with_ids(query, 8) if hasattr(facts, "search_with_ids") else []
            return "\n".join(f"#{r['id']} [{r['subject']}] {r['content']}" for r in rows) or "no matching facts"
        if action == "update":
            if kind != "fact":
                return "Only facts can be updated (episodes are historical)."
            ok = facts.update(int(id), content, subject or None)
            return f"Updated fact #{id}." if ok else f"No fact with id {id}."
        if action == "delete":
            if kind == "episode":
                # sqlite ids are ints; notion page ids are UUID strings — coerce by shape.
                rid = int(id) if str(id).isdigit() else str(id)
                return f"Deleted episode #{id}." if episodes.delete(rid) else f"No episode with id {id}."
            return f"Deleted fact #{id}." if facts.delete(int(id)) else f"No fact with id {id}."
        return "action must be one of: search, update, delete"

    return Tool(
        name="manage_memory",
        description=(
            "Search, correct, or delete the user's long-term memory (facts and episodes). "
            "ALWAYS search first to get the id, then update or delete that id. "
            "Use when the user says something you remember is wrong or should be forgotten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "update", "delete"]},
                "kind": {"type": "string", "enum": ["fact", "episode"], "description": "default fact"},
                "id": {"type": ["integer", "string"],
                       "description": "row id (from a prior search); a number for sqlite, a page id string when the notion backend is active"},
                "query": {"type": "string", "description": "keywords for search"},
                "content": {"type": "string", "description": "new text for update"},
                "subject": {"type": "string", "description": "optional new subject for a fact update"},
            },
            "required": ["action"],
        },
        fn=manage_memory,
    )


def make_update_soul_tool(settings) -> Tool:
    from waku.runtime.session import load_soul

    def update_soul(rule: str) -> str:
        rule = rule.strip().lstrip("-").strip()
        if not rule:
            return "Nothing to add."
        path = settings.home / "SOUL.md"
        text = load_soul(settings)  # ensures the file exists
        if len(text) > SOUL_MAX:
            return "SOUL.md is at its size limit — edit it in the dashboard instead."
        if "## Learned rules" not in text:
            text = text.rstrip() + "\n\n## Learned rules\n"
        text = text.rstrip() + f"\n- {rule}\n"
        path.write_text(text, encoding="utf-8")
        return f"Noted, I'll remember to: {rule}"

    return Tool(
        name="update_soul",
        description=(
            "Save a durable rule about how you should behave for this user (their "
            "preferences and standing instructions). Appends to your persona; takes "
            "effect next turn. Use when the user tells you how they want you to act."
        ),
        input_schema={
            "type": "object",
            "properties": {"rule": {"type": "string", "description": "one behaviour rule, imperative"}},
            "required": ["rule"],
        },
        fn=update_soul,
    )


def make_create_skill_tool(settings, memory) -> Tool:
    def create_skill(name: str, description: str, body: str) -> str:
        name = (name or "").strip().lower().replace(" ", "-")
        if not _SLUG.match(name):
            return "Skill name must be a short slug like 'weekly-review' (lowercase, hyphens)."
        dest = settings.home / "skills" / name / "SKILL.md"
        # never silently overwrite an existing skill (built-in or user)
        if dest.exists() or (REPO_SKILLS / name / "SKILL.md").exists():
            return f"A skill named '{name}' already exists — pick another name."
        text = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{body.strip()}\n"
        if _parse_text(text, dest) is None:
            return "That didn't validate — description must be present and non-trivial."
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        memory.skills.refresh()  # live this session
        return f"Created skill '{name}'. It will trigger on: {description.strip()}"

    return Tool(
        name="create_skill",
        description=(
            "Write a new reusable skill (a SKILL.md the agent loads when relevant) so you "
            "can repeat a workflow the user taught you. Only call this after the user agrees. "
            "body = step-by-step instructions; description = when to use it (include trigger words)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short slug, e.g. weekly-review"},
                "description": {"type": "string", "description": "one line: what it does and when to use it"},
                "body": {"type": "string", "description": "the step-by-step instructions (markdown)"},
            },
            "required": ["name", "description", "body"],
        },
        fn=create_skill,
    )
