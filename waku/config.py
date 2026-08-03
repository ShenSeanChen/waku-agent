"""Configuration — every knob is an env var, documented in .env.example.

No settings framework: a dataclass read once at startup. If you can read this
file, you know everything Waku can be configured to do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _load_env() -> str:
    """Find the user's .env the way the user expects: from where they ARE.

    Bare `load_dotenv()` searches upward from the file that called it, not from
    the working directory. Inside a git checkout that is invisible — config.py
    lives in the project, so walking up from it lands on the project's .env and
    everything works. Installed from PyPI it walks up from site-packages,
    reaches the filesystem root, and finds nothing: the user stands in a folder
    holding a perfectly good .env and Waku reports "No API key". Reported from
    a clean install on 2026-07-31, from inside the repo folder itself.

    usecwd=True is the whole fix. The walk upward is kept on purpose, so
    running `waku` from a subdirectory of your project still finds the .env at
    its root — the same rule git, npm and pytest already taught everyone.

    Returns the path that was loaded (empty string if none) so `waku doctor`
    and the first-run error can say WHICH file was read, rather than leaving
    people guessing between three .env files.
    """
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path


DOTENV_PATH = _load_env()


@dataclass
class Settings:
    # --- LLM: pick a provider, set its key. See waku/loop/models.py PROVIDERS.
    provider: str = field(default_factory=lambda: os.getenv("WAKU_PROVIDER", "anthropic"))
    # Explicit overrides (optional): key, endpoint, and model ids. Left empty,
    # the provider's own key env var and default models are used.
    api_key: str = field(default_factory=lambda: os.getenv("WAKU_API_KEY", ""))
    base_url: str | None = field(default_factory=lambda: os.getenv("WAKU_BASE_URL") or None)
    model: str = field(default_factory=lambda: os.getenv("WAKU_MODEL", ""))
    # Cheap model used by the retrieval gate and the consolidation summarizer.
    small_model: str = field(default_factory=lambda: os.getenv("WAKU_SMALL_MODEL", ""))
    # Providers the user turned off in the dashboard (comma-separated ids).
    # Disabled providers are hidden from pickers/switchers; the ACTIVE provider
    # can't be disabled (guarded in integrations.apply_provider_disabled).
    disabled_providers: frozenset[str] = field(default_factory=lambda: frozenset(
        p.strip() for p in os.getenv("WAKU_DISABLED_PROVIDERS", "").split(",") if p.strip()))

    # --- Home: where Waku keeps its state (memory DB, calendar, outbox, traces).
    # Defaults to ./.waku next to where you run it, so you can open every file
    # it writes. Local-first means you can always look.
    home: Path = field(default_factory=lambda: Path(os.getenv("WAKU_HOME", ".waku")))

    # --- Loop guardrails
    max_iterations: int = field(default_factory=lambda: int(os.getenv("WAKU_MAX_ITERATIONS", "10")))
    # Headroom matters for REASONING models (kimi-k3, gpt-5.x, gemini-*-pro):
    # they spend output tokens thinking before the answer, so a low cap makes
    # them hit stop_reason=max_tokens mid-thought and return an EMPTY reply
    # (watched kimi-k3 do exactly that at 2048). 8192 leaves room to think AND
    # answer; it's a ceiling, not a target, so efficient models still cost the same.
    max_tokens: int = field(default_factory=lambda: int(os.getenv("WAKU_MAX_TOKENS", "8192")))
    # Working memory is a SLIDING WINDOW (like context RAM): only the last N
    # turns go into the prompt. Older turns aren't lost — they're in state.db,
    # distilled into facts by consolidation, and pulled back by the retrieval
    # gate when relevant. Without this cap a long thread (esp. the always-on
    # Telegram session) resends its whole history every turn until it explodes.
    history_turns: int = field(default_factory=lambda: int(os.getenv("WAKU_HISTORY_TURNS", "12")))

    # --- Memory
    # Consolidate (distill chats into durable facts) only after N new exchanges.
    consolidate_every: int = field(default_factory=lambda: int(os.getenv("WAKU_CONSOLIDATE_EVERY", "6")))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("WAKU_RETRIEVAL_TOP_K", "4")))
    # 'sqlite' (default, zero setup) or 'supabase' (pgvector upgrade path — see launch-rag).
    semantic_store: str = field(default_factory=lambda: os.getenv("WAKU_SEMANTIC_STORE", "sqlite"))
    # 'sqlite' (default, zero setup) or 'notion' (episodes live in a Notion database).
    episodic_store: str = field(default_factory=lambda: os.getenv("WAKU_EPISODIC_STORE", "sqlite"))

    # --- Tools
    # Sync created events into Apple Calendar (a dedicated "Waku" calendar)
    # via AppleScript. Opt-in because it writes to your real calendar app.
    apple_calendar: bool = field(
        default_factory=lambda: os.getenv("WAKU_APPLE_CALENDAR", "") in ("1", "true", "yes")
    )
    # Mirror locally-created events to Google Calendar. SQLite + ICS remain the
    # source of truth; this is only an opt-in write target.
    google_calendar: bool = field(
        default_factory=lambda: os.getenv("WAKU_GOOGLE_CALENDAR", "") in ("1", "true", "yes")
    )
    google_calendar_id: str = field(
        default_factory=lambda: os.getenv("WAKU_GOOGLE_CALENDAR_ID", "") or "primary"
    )
    # Give the agent read/write access to Apple Calendar, Mail, Reminders, Notes
    # (macOS; first use triggers the system Automation permission prompts).
    apple_tools: bool = field(
        default_factory=lambda: os.getenv("WAKU_APPLE_TOOLS", "") in ("1", "true", "yes")
    )
    # Read-only GitHub access through the `gh` CLI's own auth (no token here).
    # Off by default and deliberately so: every registered tool ships in every
    # prompt, and reading PRs is maintainer capability, not assistant capability.
    # The gather workflow calls waku/tools/github.py as a library and does NOT
    # need this on — the switch only decides whether the MODEL can reach it.
    gh_tool: bool = field(
        default_factory=lambda: os.getenv("WAKU_GH_TOOL", "") in ("1", "true", "yes")
    )
    # owner/name to assume when a call omits it — for when Waku runs outside a
    # checkout, where `gh` has no remote to infer from.
    gh_repo: str = field(default_factory=lambda: os.getenv("WAKU_GH_REPO", ""))
    # Register the experimental tools (delegate_task -> pi sub-agent, ...). Env is
    # the global switch; the arena sets this per-race so a coding race can hand
    # work to pi WITHOUT flipping it on for the whole process.
    experimental: bool = field(
        default_factory=lambda: os.getenv("WAKU_EXPERIMENTAL", "") in ("1", "true", "yes")
    )
    # Route every message through the triage graph workflow first (a small model
    # classifies it; trivial messages get a fast small-model reply, real tasks
    # run the normal loop as a graph node). Any failure anywhere fails open to
    # the plain loop, so this can never make Waku worse — only faster/cheaper.
    graph_workflows: bool = field(
        default_factory=lambda: os.getenv("WAKU_GRAPH_WORKFLOWS", "") in ("1", "true", "yes")
    )

    # --- Optional gateway
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    whatsapp_token: str = field(default_factory=lambda: os.getenv("WHATSAPP_TOKEN", ""))
    whatsapp_phone_number_id: str = field(
        default_factory=lambda: os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    )

    # --- Tracing (JSONL always; OTel exports if an endpoint is set)
    otel_endpoint: str = field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    )

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "traces").mkdir(exist_ok=True)
        (self.home / "outbox").mkdir(exist_ok=True)
        return self.home


def load_settings() -> Settings:
    return Settings()
