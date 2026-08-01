"""search_web — the second real tool, and a great LOOP demo.

"Find the World Cup games left and put them on my calendar" makes the agent
loop across tools: search_web (read the web) → reason over the results →
create_event once per match. Watch the LOOP box cycle on the dashboard.

Zero new dependencies — just stdlib urllib. Two backends:
  default  DuckDuckGo HTML (no key, no setup — good enough to demo)
  better   Tavily, if TAVILY_API_KEY (or MILLI_SEARCH_API_KEY) is set — an
           agent-friendly search API with cleaner results (free tier)

The tool returns plain text the model reads; it never parses HTML for the model.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request

from milli.tools.registry import Tool

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _tavily(query: str, key: str, max_results: int) -> list[tuple[str, str, str]]:
    body = json.dumps({"api_key": key, "query": query, "max_results": max_results,
                       "include_answer": False}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return [(r.get("title", ""), (r.get("content", "") or "")[:400], r.get("url", ""))
            for r in data.get("results", [])]


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
        key = os.getenv("TAVILY_API_KEY") or os.getenv("MILLI_SEARCH_API_KEY")
        try:
            results = _tavily(query, key, max_results) if key else _duckduckgo(query, max_results)
        except Exception as exc:
            results = None if key else []  # DDG blocked → fall through to the hint below
            if key:
                return f"Web search failed ({exc}). Answer from what you know, or ask the user."
        if not results:
            if not key:
                return ("No results — DuckDuckGo's free endpoint often blocks automated "
                        "requests. For reliable search set a free TAVILY_API_KEY in .env "
                        "(https://tavily.com); see .env.example. Meanwhile, tell the user "
                        "you couldn't search and ask them to add the key.")
            return "No results found. Try a more specific query."
        engine = "Tavily" if key else "DuckDuckGo"
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
