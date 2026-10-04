"""Research reads the company brain first (spec 009 A).

A research turn used to start from nothing: every hosted turn showed "Used 0",
and the second time Sean asked about mem0's competitors the agent repeated the
whole search instead of starting from the first report. The research is in
Waku Memory, which only an MCP search reaches, so the agent's own code runs
that search before the model's first call, every research turn, instead of
hoping the model thinks of it.

When a turn is research (the research-report skill matched the message) and
Waku Memory is connected, this module:

  1. runs memory.search twice: once for the subject, in every scope, and once
     for earlier reports, kind `semantic` (the kind spec 007 saves a report
     as), so an earlier report is found even when facts crowd the first list,
  2. hands back what it found as the turn's Used list, plus each call as a
     tool event, the same shape the loop emits for a model's own
     waku_memory_memory_search, which is what waku.one's panel already reads,
  3. writes the "What the company brain already knows" block for the system
     prompt, each memory with its date and id.

A search that fails is logged and skipped: reading first is a help, and a
turn never waits on it or fails for it. Like consolidation and reports, this
module never sees the transport; app.py passes search_via()'s callable, so a
fake stands in for it in the evals.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from waku.memory.reports import KIND as REPORT_KIND
from waku.memory.reports import MARKER

# The skill whose trigger marks a turn as research. Its matcher (keyword
# overlap with the skill's name and description) is the detector: no second
# heuristic to drift from what the skill itself answers to.
RESEARCH_SKILL = "research-report"

HEADING = "What the company brain already knows"

# The tool name the model would call for the same search: the loop names MCP
# tools <server>_<tool>, and waku.one reads entries out of this one as Used.
TOOL = "waku_memory_memory_search"

SUBJECT_LIMIT = 6
REPORT_LIMIT = 3
# What one memory may put in the prompt. A report is longer, and its id is
# given, so the model can read the whole with waku_memory_memory_get.
TEXT_CHARS = 600

# Words that say "do research", not what about. What is left is the subject.
_FILLER_WORDS = (
    "a an the of for on about and or to in into with vs versus me my our us we i you "
    "please can could would will do does did tell show give find look up research "
    "researching compare comparing who what which whats how is are was "
    "were be latest new current report write make get some any all")
_FILLER = frozenset(_FILLER_WORDS.split())

Search = Callable[[dict], str]

log = logging.getLogger(__name__)


@dataclass
class ReadFirst:
    calls: list[dict] = field(default_factory=list)   # tool events, one per search
    used: list[dict] = field(default_factory=list)    # {id, text, created_at, kind, report}
    context: str = ""                                 # the system prompt's block, or ""


def is_research(matched_skills) -> bool:
    return any(getattr(s, "name", None) == RESEARCH_SKILL for s in matched_skills)


def subject(message: str) -> str:
    """The message without the words that only ask for research:
    "research the competitors of mem0" -> "competitors mem0"."""
    words = re.findall(r"[\w.+-]+", message.lower())
    kept = [w.strip(".") for w in words if w.strip(".") and w.strip(".") not in _FILLER]
    return " ".join(kept) or message.strip()


def _entries(text: str) -> list[dict]:
    try:
        entries = json.loads(text).get("entries", [])
    except (ValueError, AttributeError):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _is_report(entry: dict) -> bool:
    body = str(entry.get("body") or "")
    return body.lstrip().startswith(MARKER) or entry.get("kind") == REPORT_KIND


def _title(entry: dict) -> str:
    """A report's `# ` line, when the body that came back still has it."""
    for line in str(entry.get("body") or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _text(entry: dict) -> str:
    body = str(entry.get("body") or entry.get("snippet") or "").replace(MARKER, "").strip()
    body = " ".join(body.split())
    return body if len(body) <= TEXT_CHARS else body[:TEXT_CHARS - 1] + "…"


def read_first(message: str, search: Search | None) -> ReadFirst:
    """Both searches, what they found, and the prompt block. Empty when
    `search` is None (Waku Memory not connected) and when nothing was found."""
    out = ReadFirst()
    if search is None:
        return out
    about = subject(message)
    asks = [{"query": about, "scope": "all", "limit": SUBJECT_LIMIT},
            {"query": about, "kind": REPORT_KIND, "scope": "all", "limit": REPORT_LIMIT}]
    seen: set[str] = set()
    for args in asks:
        started = time.perf_counter()
        try:
            text = search(args)
        except Exception as exc:
            log.warning("Waku Memory search before research failed (%s); "
                        "the turn goes on without it", exc)
            continue
        out.calls.append({"tool": TOOL, "args": args, "output": text, "read_first": True,
                          "duration_ms": int((time.perf_counter() - started) * 1000)})
        for entry in _entries(text):
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            report = _is_report(entry)
            out.used.append({"id": entry["id"], "text": _text(entry),
                             "created_at": str(entry.get("created_at") or "")[:10],
                             "kind": entry.get("kind"), "report": report,
                             "title": _title(entry) if report else ""})
    out.context = context(out.used)
    return out


def context(used: list[dict]) -> str:
    """The block the model reads before it researches. Reports first, since
    a report is the thing to start from, then everything else, newest first."""
    if not used:
        return ""
    newest = sorted(used, key=lambda u: u["created_at"], reverse=True)
    ordered = sorted(newest, key=lambda u: not u["report"])   # stable: reports first
    lines = [(f"\n{HEADING} (from Waku Memory, searched before this turn; "
              "each item has the date it was saved):")]
    for u in ordered:
        when = u["created_at"] or "date unknown"
        if u["report"]:
            name = f'"{u["title"]}"' if u["title"] else "an earlier report"
            lines.append(f"- Report {name}, saved {when} (memory {u['id']}; "
                         f"waku_memory_memory_get returns all of it): {u['text']}")
        else:
            lines.append(f"- {when}: {u['text']} (memory {u['id']})")
    lines.append("Start from this. Name an earlier report and its date when you use it, "
                 "and research only what is missing here or older than 30 days.")
    return "\n".join(lines)

