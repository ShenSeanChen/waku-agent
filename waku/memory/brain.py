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
  3. reads each earlier report it found (memory.get, at most REPORT_LIMIT)
     and keeps only its digest: title, Summary and key numbers, at most
     reports.DIGEST_CHARS. A search answers with a snippet of a long body,
     so without this the model fetched every whole report itself, and each
     rode along on every later call of the turn (2026-10-05: 328.5k tokens
     in, twice an earlier turn's),
  4. writes the "What the company brain already knows" block for the system
     prompt, each memory with its date and id, and each report as its digest.

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
from waku.memory.reports import MARKER, digest

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
# What one memory may put in the prompt. A report puts in its digest
# (reports.DIGEST_CHARS) instead, and its id is given, so the model can read
# the whole with waku_memory_memory_get when the person asks for details.
TEXT_CHARS = 600

# Words that say "do research", not what about. What is left is the subject.
_FILLER_WORDS = (
    "a an the of for on about and or to in into with vs versus me my our us we i you "
    "please can could would will do does did tell show give find look up research "
    "researching compare comparing who what which whats how is are was "
    "were be latest new current report write make get some any all "
    "save brief summary snapshot using")
_FILLER = frozenset(_FILLER_WORDS.split())

Search = Callable[[dict], str]
Get = Callable[[str], str]

log = logging.getLogger(__name__)


@dataclass
class ReadFirst:
    calls: list[dict] = field(default_factory=list)   # tool events, one per search
    used: list[dict] = field(default_factory=list)    # {id, text, created_at, kind, report}
    context: str = ""                                 # the system prompt's block, or ""
    digests: dict = field(default_factory=dict)       # report id -> its digest


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


def _raw(entry: dict) -> str:
    return str(entry.get("body") or entry.get("snippet") or "")


def _title(body: str) -> str:
    """A report's `# ` line, when the body that came back still has it."""
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _first_summary_line(body: str) -> str:
    """The first bullet under a report's `## Summary`, when the body that
    came back still has that section."""
    inside = False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == "summary"
        elif inside and line.lstrip().startswith(("- ", "* ")):
            return line.lstrip()[2:].strip()
    return ""


# A search snippet is a window into a body, and for a report the window often
# lands in its Sources or metrics block: `cognee.ai\"}, "url": "https://...`.
# Prose keeps Waku Memory's <<match>> marks; these go.
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LONE_FENCE = re.compile(r"```\w*")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_JSON_PAIR = re.compile(
    r"\\?\"?[\w$.\- ]*\\?\"\s*:\s*(?:\\?\"(?:[^\"\\]|\\.)*?\\?\"|-?[\d.]+|true|false|null)")
_JSON_DEBRIS = re.compile(r"\\?\"\s*[}\]]+|[{}\[\]]+|\\\"|(?<=\s),(?=\s)|^\s*[,:]+")


def _prose(text: str) -> str:
    """`text` without fenced code, comments (the report marker among them)
    and JSON fragments, on one line."""
    text = _LONE_FENCE.sub(" ", _FENCE.sub(" ", _COMMENT.sub(" ", text)))
    text = _JSON_DEBRIS.sub(" ", _JSON_PAIR.sub(" ", text))
    text = re.sub(r"\s+([,;.])", r"\1", " ".join(text.split()))
    text = re.sub(r"[,;](?:\s*[,;])+", ",", text)
    return text.strip(" ,;:")


def _clip(text: str) -> str:
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS - 1] + "…"


def _text(entry: dict) -> str:
    """What a memory says, for the Used list and the prompt: a fact's prose,
    and a report's first Summary line (its title is shown beside it)."""
    raw = _raw(entry)
    if _is_report(entry):
        return _clip(_first_summary_line(raw))
    return _clip(_prose(raw))


def _report_body(get: Get, memory_id: str) -> str:
    """One earlier report's whole body, read with memory.get, or "" when the
    read failed or the memory is not a report."""
    try:
        answer = json.loads(get(memory_id))
    except Exception as exc:
        log.warning("Waku Memory get of report %s before research failed (%s); "
                    "its snippet stands in", memory_id, exc)
        return ""
    if not isinstance(answer, dict):
        return ""
    memory = answer.get("memory") if isinstance(answer.get("memory"), dict) else answer
    body = str(memory.get("body") or "")
    return body if body.lstrip().startswith(MARKER) else ""


def read_first(message: str, search: Search | None, get: Get | None = None) -> ReadFirst:
    """Both searches, what they found, and the prompt block. Empty when
    `search` is None (Waku Memory not connected) and when nothing was found.
    With `get`, each report found is read once and put in as its digest."""
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
                             "title": _title(_raw(entry)) if report else ""})
    if get is not None:
        for u in [u for u in out.used if u["report"]][:REPORT_LIMIT]:
            body = _report_body(get, u["id"])
            if body:
                # The whole report: its own title and first Summary line, in
                # place of whatever window of it the search snippet showed.
                out.digests[u["id"]] = digest(body)
                u["title"] = _title(body) or u["title"]
                u["text"] = _clip(_first_summary_line(body)) or u["text"]
    out.context = context(out.used, out.digests)
    return out


def context(used: list[dict], digests: dict | None = None) -> str:
    """The block the model reads before it researches. Reports first, since
    a report is the thing to start from, then everything else, newest first."""
    if not used:
        return ""
    newest = sorted(used, key=lambda u: u["created_at"], reverse=True)
    ordered = sorted(newest, key=lambda u: not u["report"])   # stable: reports first
    lines = [(f"\n{HEADING} (from Waku Memory, searched before this turn; "
              "each item has the date it was saved):")]
    digests = digests or {}
    for u in ordered:
        when = u["created_at"] or "date unknown"
        if u["report"]:
            name = f'"{u["title"]}"' if u["title"] else "an earlier report"
            summary = digests.get(u["id"])
            text = ("its summary:\n  " + summary.replace("\n", "\n  ")) if summary else u["text"]
            lines.append(f"- Report {name}, saved {when} (memory {u['id']}): {text}")
        else:
            lines.append(f"- {when}: {u['text']} (memory {u['id']})")
    lines.append("Start from this. Name an earlier report and its date when you use it, "
                 "and research only what is missing here or older than 30 days.")
    if any(u["report"] for u in used):
        lines.append("A report's summary here is enough to start from. Call "
                     "waku_memory_memory_get for a whole earlier report only when the "
                     "person asks to compare details with it.")
    return "\n".join(lines)

