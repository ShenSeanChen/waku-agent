"""search_web — the second real tool, and a great LOOP demo.

"Find the World Cup games left and put them on my calendar" makes the agent
loop across tools: search_web (read the web) → reason over the results →
create_event once per match. Watch the LOOP box cycle on the dashboard.

Zero new dependencies — just stdlib urllib. Three backends:
  default   DuckDuckGo HTML (no key, no setup — good enough to demo)
  Tavily    if TAVILY_API_KEY (or WAKU_SEARCH_API_KEY) is set
  Firecrawl if FIRECRAWL_API_KEY is set — POST /v2/search

A Firecrawl key means use Firecrawl (that's why you pasted it). Otherwise Tavily,
otherwise DuckDuckGo. WAKU_SEARCH_BACKEND=tavily|firecrawl|duckduckgo is an
optional .env override, not a dashboard control.

The tool returns plain text the model reads; it never parses HTML for the model.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request

from waku.tools.registry import Tool

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v2/search"


def _tavily_key() -> str:
    return (os.getenv("TAVILY_API_KEY") or os.getenv("WAKU_SEARCH_API_KEY") or "").strip()


def _firecrawl_key() -> str:
    return os.getenv("FIRECRAWL_API_KEY", "").strip()


def resolve_backend() -> str:
    """Which engine search_web will actually call.

    Filling FIRECRAWL_API_KEY is the dashboard signal to use Firecrawl; Tavily
    remains the paid fallback when that key is absent.
    """
    requested = os.getenv("WAKU_SEARCH_BACKEND", "").strip().lower()
    if requested in ("tavily", "firecrawl", "duckduckgo"):
        return requested
    if _firecrawl_key():
        return "firecrawl"
    if _tavily_key():
        return "tavily"
    return "duckduckgo"


def _tavily(query: str, key: str, max_results: int) -> list[tuple[str, str, str]]:
    body = json.dumps({"api_key": key, "query": query, "max_results": max_results,
                       "include_answer": False}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return [(r.get("title", ""), (r.get("content", "") or "")[:400], r.get("url", ""))
            for r in data.get("results", [])]


def _firecrawl_rows(payload: object) -> list[dict]:
    """v2 search has come back as a list, {data: [...]}, or {data: {web: [...]}}."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        web = data.get("web") or data.get("results") or []
        if isinstance(web, list):
            return [row for row in web if isinstance(row, dict)]
    return []


def _firecrawl(query: str, key: str, max_results: int) -> list[tuple[str, str, str]]:
    body = json.dumps({"query": query, "limit": max_results}).encode()
    req = urllib.request.Request(
        _FIRECRAWL_SEARCH, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    out = []
    for row in _firecrawl_rows(payload)[:max_results]:
        snippet = (row.get("description") or row.get("markdown")
                   or row.get("content") or "")
        out.append((row.get("title", ""), snippet[:400], row.get("url", "")))
    return out


def _strip(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _duckduckgo(query: str, max_results: int) -> list[tuple[str, str, str]]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", "ignore")
    links = re.findall(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.DOTALL)
    snips = re.findall(r'result__snippet"[^>]*>(.*?)</a>', page, re.DOTALL)
    out = []
    for i, (href, title) in enumerate(links[:max_results]):
        target = href
        m = re.search(r"uddg=([^&]+)", href)  # DDG wraps results in a redirect
        if m:
            target = urllib.parse.unquote(m.group(1))
        out.append((_strip(title), _strip(snips[i]) if i < len(snips) else "", target))
    return out


def make_tool() -> Tool:
    def search_web(query: str, max_results: int = 5) -> str:
        backend = resolve_backend()
        tavily_key, firecrawl_key = _tavily_key(), _firecrawl_key()
        if backend == "tavily" and not tavily_key:
            return "Web search is set to Tavily but TAVILY_API_KEY is empty. Add the key."
        if backend == "firecrawl" and not firecrawl_key:
            return "Web search is set to Firecrawl but FIRECRAWL_API_KEY is empty. Add the key."
        labels = {"tavily": "Tavily", "firecrawl": "Firecrawl", "duckduckgo": "DuckDuckGo"}
        engine = labels[backend]
        try:
            if backend == "tavily":
                results = _tavily(query, tavily_key, max_results)
            elif backend == "firecrawl":
                results = _firecrawl(query, firecrawl_key, max_results)
            else:
                results = _duckduckgo(query, max_results)
        except Exception as exc:
            if backend == "duckduckgo":
                results = []
            else:
                return f"Web search failed ({exc}). Answer from what you know, or ask the user."
        if not results:
            if backend == "duckduckgo":
                return ("No results — DuckDuckGo's free endpoint often blocks automated "
                        "requests. For reliable search set TAVILY_API_KEY or "
                        "FIRECRAWL_API_KEY in .env; see .env.example. Meanwhile, tell "
                        "the user you couldn't search and ask them to add a key.")
            return "No results found. Try a more specific query."
        lines = [f"Web results for '{query}' (via {engine}):"]
        for i, (title, snippet, link) in enumerate(results, 1):
            lines.append(f"{i}. {title}\n   {snippet}\n   {link}")
        return "\n".join(lines)

    return Tool(
        name="search_web",
        description=(
            "Search the public web and get back the top results (title, snippet, URL). "
            "Use when the user asks about current events, facts, schedules, or anything "
            "you don't already know — then act on what you find (e.g. create calendar events)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "the search query"},
                "max_results": {"type": "integer", "description": "how many results (default 5)"},
            },
            "required": ["query"],
        },
        fn=search_web,
    )
