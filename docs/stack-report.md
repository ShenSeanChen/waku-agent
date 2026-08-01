# Stack report — verified 2026-07-10

The brief (§7) asked for a research-then-verify pass before building. Result: every layer
below was installed together in one venv (Python 3.13, uv) and smoke-tested end-to-end.

## Chosen stack (and what was rejected)

| Layer | Choice | Verified version | Rejected & why |
|---|---|---|---|
| Loop / harness | Hand-rolled ~150-line loop on the Anthropic SDK | `anthropic 0.116.0` | PydanticAI (great, but hides the loop we're teaching); smolagents (HF experimental tier); PocketFlow (graph abstraction, not a loop); LangGraph via launch-DeepResearch-Backend (drags in the whole LangChain stack — see below) |
| Memory | Hand-built 3 pillars on SQLite + FTS5 (stdlib) | Python 3.13 stdlib, FTS5 confirmed | mem0 / Letta / Zep-Graphiti black-box exactly what the video teaches (consolidation + retrieval gating). Listed in README as production alternatives |
| Vector upgrade path | Supabase pgvector adapter (lifted from launch-agentic-rag) | optional extra `[supabase]` | Chroma/Qdrant: another service to run; sqlite-vec: still pre-v1 alpha |
| Eval — deterministic | plain pytest assertions on tool calls | `pytest 9.1.1` | — (this side must NOT use an LLM; that's the point) |
| Eval — LLM-as-judge | DeepEval (GEval), pytest-native | `deepeval 4.0.8` | promptfoo: strong assertions but YAML-driven + Node CLI, less teachable in a Python repo |
| Tracing / LLM Ops | JSONL trace per run + OTel spans → Arize Phoenix locally | `arize-phoenix 17.23.0`, OTLP gRPC | Langfuse v3 self-host needs ~6 containers (web, worker, Postgres, ClickHouse, Redis, MinIO) — too heavy for clone-and-run. Langfuse **cloud** still works via the same OTel env toggle |
| Gateway | CLI REPL default; Telegram long-polling optional | extra `[telegram]` (`python-telegram-bot >=21`) | WhatsApp: Meta Business Cloud API needs business verification + public webhook — deferred as a community contribution |

## Smoke tests run (all passing)

1. **SQLite FTS5**: virtual table + `MATCH` ranked query works in stdlib `sqlite3` — no install.
2. **Phoenix + OTel**: `python -m phoenix.server.main serve` came up on `localhost:6006` in ~1s;
   a fake `agent_run → retrieval_gate → tool.create_event` span tree exported over OTLP
   (`localhost:4317`) and appeared via the Phoenix API (`traceCount: 1`).
3. **DeepEval**: custom deterministic `BaseMetric` (no LLM) measures and passes; `GEval`
   imports cleanly for the judge suite.
4. **Anthropic SDK**: imports at 0.116.0. Live tool-use round-trip pending an
   `ANTHROPIC_API_KEY` in `.env` (deliberately not sourced from other repos' secrets).

## Why not extend launch-DeepResearch-Backend's loop (brief asked)

Explored in depth: it is a LangGraph `StateGraph` fork of `open_deep_research` — supervisor
fan-out, research-specific state and prompts, hard LangChain coupling. The *pattern* worth
keeping (tools-by-name dict, parallel dispatch with `asyncio.gather`, safe-execute wrapper)
is reimplemented here in plain Python in `milli/loop/agent.py`.
