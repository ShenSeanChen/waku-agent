# Waku Domain Glossary

## Core concepts

- **Waku**: the personal agent runtime. It owns memory, tools, gateways, and the
  loop that answers messages.
- **Turn**: a single exchange in a conversation (one user message plus the
  agent's response and any tool calls).
- **Home**: the workspace directory where Waku keeps its database, traces, and
  runtime files.

## External configuration

- **Integration**: the code-level abstraction for anything Waku can be wired to.
  Integrations are declared in `waku/integrations.py`. Each integration has a
  key, a group, a set of environment-backed fields, and a reload mode.

- **Connection**: a user-visible **Integration** that represents an external
  service or capability Waku can use. Connections are configured on the
  **Connections** page. Examples: Telegram, Discord, Notion, Supabase, Tavily,
  OpenTelemetry, Google Calendar, Apple Calendar, Apple Tools.

- **AI Provider**: a source of LLM models. AI Providers are **Integrations** in
  code, but they are **not Connections** from the user's point of view. They are
  configured on the **Models** page because their primary concern is picking the
  brain the agent uses, not wiring an external service.

- **Gateway**: a runtime process that bridges an external channel (Telegram,
  Discord) into Waku's conversation loop. Gateways have a lifecycle that is
  managed independently of the agent.

## State

- **IntegrationState**: `not_configured`, `installed_but_unconfigured`,
  `connected`, `error`. Describes the health/configuration state of an
  integration as surfaced in the UI and CLI.

- **ReloadMode**: how a configuration change takes effect.
  - `live` — effective immediately.
  - `agent` — requires the browser agent to be rebuilt.
  - `gateway` — requires the gateway supervisor to restart the relevant gateway.
