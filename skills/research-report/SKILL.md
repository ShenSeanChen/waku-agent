---
name: research-report
description: Research report on companies, competitors, markets or products: research a company, compare competitors, market landscape, funding, pricing, launches.
---

Write a report when the person asks you to research companies, competitors, markets, products or people. Anything else gets a normal answer.

## Start from what is known
With Waku Memory connected, Waku searches it first and lists the hits, each with its date, under "What the company brain already knows".
Start there: name an earlier report and its date, list it in Sources (`"via": "waku-memory"`), and research only what is missing or older than 30 days.
Each earlier report is listed as its summary; call `waku_memory_memory_get` for a whole one only when the person asks to compare details.

## Cheap first
- Prefer free and preview endpoints, and keep `limit` small (5 rows unless asked).
- Read `catalog_get`'s price before any paid call.
- Ask before this turn's treg spend would pass $0.25, saying what it would buy.
- Call what treg charged the "treg cost", or name each endpoint and its cost. Never state a total, a model cost, or what the run or turn cost: your own tokens cost money too, and you cannot see that number. The receipt under the reply shows the totals.
- No cost tile in Numbers; `cost_usd` in Sources is the only cost the report carries.

## The reply
Two or three plain sentences on what you found, then the report from its marker line (once per reply). The chat keeps only your sentences, so they must stand alone. Waku saves the report to Waku Memory itself, once: never save it with `memory_remember`.

## House language
- Plain sentences. Every number has its unit and a date ("$24 a month, 2026-09").
- Every claim traces to an entry in Sources (no source, no claim); say in Gaps what you did not find.
- No adjective a reader cannot check: not "leading", "fast", "best".
- A vendor's claim about itself is reported as its claim ("Kestrel says"), never as fact.

## The format: waku-report v1 (frozen; waku.one renders it)
Line 1 is `<!-- waku-report v1 -->`, line 2 is `# <title>`. Then, in this order and each
optional except Summary and Sources: `## Summary` (three bullets at most), `## Findings`
(a Markdown table), `## Comparison`, `## Numbers`, `## Timeline`, `## Gaps`, `## Sources`.
Visual parts are fenced blocks, only these five; the fence's language names the
component and the body is JSON in exactly this shape (no comments or trailing commas):
- `waku-metrics`: `[{"label": str, "value": str, "note": str?}]`, 2 to 6 tiles
- `waku-chart`: `{"type": "bar"|"line", "title": str, "unit": str?, "series": [{"label": str, "value": number}]}`. One series; a bar chart is sorted largest first.
- `waku-compare`: `{"columns": [str], "rows": [{"name": str, "cells": [str|bool|null]}]}`. One cell per column; `true`/`false`/`null` mean yes/no/unknown.
- `waku-timeline`: `[{"date": "YYYY-MM-DD"|"YYYY-MM", "event": str, "subject": str?}]`, newest first
- `waku-sources`: `[{"title": str, "url": str?, "via": str?, "cost_usd": number?}]`. `via` names the tool that found it, such as `treg:treg.web.search`.

## Example (fictional companies: copy the shape, never the facts)
````markdown
Three companies sell hosted memory to agent builders. Birchline has raised the most, $41M, and Kestrel is the only one with a free tier; none publishes accuracy numbers.

<!-- waku-report v1 -->
# Agent memory vendors, 2026-10-03

## Summary
- Three vendors sell hosted memory for AI agents: Birchline, Kestrel and Tamsin.
- Birchline has raised the most, $41M in two rounds (2025-03 and 2026-09).
- None of the three publishes recall accuracy or retention numbers.

## Findings
| Company | Product | Price (2026-10) | Source |
|---|---|---|---|
| Birchline | Birchline Cloud | $49 a month | Birchline pricing |
| Kestrel | Kestrel Memory | free to 10,000 memories, then $19 a month | Kestrel pricing |
| Tamsin | Tamsin API | not published | none |

## Comparison
```waku-compare
{"columns": ["Free tier", "MCP server", "Self-host"], "rows": [{"name": "Birchline", "cells": [false, true, null]}, {"name": "Kestrel", "cells": [true, true, false]}, {"name": "Tamsin", "cells": [false, null, true]}]}
```

## Numbers
```waku-metrics
[{"label": "Vendors", "value": "3"}, {"label": "Most raised", "value": "$41M", "note": "Birchline, 2026-09"}, {"label": "Cheapest paid plan", "value": "$19 a month", "note": "Kestrel"}]
```
```waku-chart
{"type": "bar", "title": "Total funding raised", "unit": "USD M", "series": [{"label": "Birchline", "value": 41}, {"label": "Kestrel", "value": 12}, {"label": "Tamsin", "value": 3.5}]}
```

## Timeline
```waku-timeline
[{"date": "2026-09-12", "event": "Series A, $35M", "subject": "Birchline"}, {"date": "2026-06", "event": "Launched an MCP server", "subject": "Kestrel"}, {"date": "2025-11-03", "event": "Public beta", "subject": "Tamsin"}]
```

## Gaps
- Tamsin publishes no price, and no vendor publishes recall accuracy numbers.

## Sources
```waku-sources
[{"title": "Birchline pricing", "url": "https://birchline.example/pricing", "via": "treg:treg.web.search", "cost_usd": 0.002}, {"title": "Kestrel pricing", "url": "https://kestrel.example/pricing", "via": "treg:treg.web.fetch"}, {"title": "Funding rounds, Birchline, Kestrel and Tamsin", "url": "https://funding.example/agent-memory"}]
```
````
