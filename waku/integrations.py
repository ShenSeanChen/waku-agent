"""Single registry for every user-configurable connection.

The declarations in this module intentionally have no I/O.  The read, write
and probe operations below the registry are added here too so all surfaces can
share one small public API.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from waku.loop.models import PROVIDERS, Provider
from waku.memory.episodic.notion_store import normalize_database_id


class IntegrationState(StrEnum):
    """Where an integration stands, from empty to proven working.

    CONFIGURED exists because the other four could not tell two very different
    situations apart. "Telegram is missing its token" and "Telegram has
    everything and I simply have not probed it yet" both returned
    INSTALLED_BUT_UNCONFIGURED, and the dashboard renders that as "needs
    setup" — so a working Tavily key, a live Telegram gateway and a connected
    Notion database all told the user they needed setting up. The health store
    is empty on a fresh checkout, which means EVERY user saw that on their
    first visit, about integrations that were already working.
    """

    NOT_CONFIGURED = "not_configured"                        # nothing filled in
    INSTALLED_BUT_UNCONFIGURED = "installed_but_unconfigured"  # something REQUIRED is missing
    CONFIGURED = "configured"                                # complete, never tested
    CONNECTED = "connected"                                  # probed, and it worked
    ERROR = "error"


class ReloadMode(StrEnum):
    LIVE = "live"
    AGENT = "agent"
    GATEWAY = "gateway"


class FieldKind(StrEnum):
    TEXT = "text"
    BOOL = "bool"
    CHOICE = "choice"


@dataclass(frozen=True)
class EnvField:
    name: str
    label: str
    kind: FieldKind = FieldKind.TEXT
    required: bool = False
    secret: bool = False
    default: str = ""
    options: tuple[str, ...] = ()
    placeholder: str = ""
    help: str = ""
    option_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationStatus:
    state: IntegrationState
    message: str = ""
    checked_at: str | None = None


@dataclass(frozen=True)
class Integration:
    key: str
    group: str
    name: str
    what: str
    env: tuple[EnvField, ...]
    extra: str | None
    import_name: str | None
    setup_url: str
    reload: ReloadMode
    enabled: Callable[[Mapping[str, str]], bool]
    probe: Callable[[Mapping[str, str]], None] | None
    normalize: Callable[[dict[str, str]], dict[str, str]] | None = None


@dataclass(frozen=True)
class FieldView:
    name: str
    label: str
    kind: FieldKind
    required: bool
    secret: bool
    configured: bool
    last4: str
    options: tuple[str, ...] = ()
    value: str = ""
    help: str = ""
    option_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationView:
    key: str
    group: str
    name: str
    what: str
    status: IntegrationStatus
    fields: tuple[FieldView, ...]
    install_command: str
    setup_url: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    view: IntegrationView | None = None
    error: str = ""
    can_force: bool = False


def _positive_int(values: dict[str, str]) -> dict[str, str]:
    value = values.get("DISCORD_MAX_TURNS_PER_HOUR", "")
    if value:
        try:
            if int(value) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("DISCORD_MAX_TURNS_PER_HOUR must be a positive integer") from None
    return values


def _notion_normalize(values: dict[str, str]) -> dict[str, str]:
    value = values.get("NOTION_EPISODES_DATABASE_ID", "")
    if value:
        values["NOTION_EPISODES_DATABASE_ID"] = normalize_database_id(value)
    return values


def _darwin(env: Mapping[str, str]) -> bool:
    return sys.platform == "darwin" and bool(env.get("WAKU_APPLE_CALENDAR"))


def _darwin_tools(env: Mapping[str, str]) -> bool:
    return sys.platform == "darwin" and bool(env.get("WAKU_APPLE_TOOLS"))


INTEGRATIONS: tuple[Integration, ...] = (
    Integration("telegram", "Channels", "Telegram", "Lets Waku receive and send Telegram messages.",
                (EnvField("TELEGRAM_BOT_TOKEN", "Bot token", required=True, secret=True),
                 EnvField("TELEGRAM_ALLOWED_USER", "Allowed user")), "telegram", "telegram",
                "https://t.me/BotFather", ReloadMode.GATEWAY,
                lambda env: bool(env.get("TELEGRAM_BOT_TOKEN")), None),
    Integration("discord", "Channels", "Discord", "Lets Waku answer in a Discord server.",
                (EnvField("DISCORD_BOT_TOKEN", "Bot token", required=True, secret=True),
                 EnvField("DISCORD_ALLOWED_USER", "Allowed user"),
                 EnvField("DISCORD_ALLOWED_CHANNEL", "Allowed channel"),
                 EnvField("DISCORD_REQUIRE_MENTION", "Require mention", FieldKind.BOOL),
                 EnvField("DISCORD_MAX_TURNS_PER_HOUR", "Max turns per hour"),
                 EnvField("DISCORD_HOME", "Workspace directory")), "discord", "discord", "",
                ReloadMode.GATEWAY, lambda env: bool(env.get("DISCORD_BOT_TOKEN")), None,
                _positive_int),
    Integration("whatsapp", "Channels", "WhatsApp", "Lets Waku answer WhatsApp messages via the Meta Cloud API.",
                (EnvField("WHATSAPP_TOKEN", "Access token", required=True, secret=True),
                 EnvField("WHATSAPP_PHONE_NUMBER_ID", "Phone number ID", required=True),
                 EnvField("WHATSAPP_APP_SECRET", "App secret", required=True, secret=True),
                 EnvField("WHATSAPP_VERIFY_TOKEN", "Webhook verify token", required=True, secret=True),
                 EnvField("WHATSAPP_ALLOWED_PHONE", "Allowed phone",
                          help="Without '+' prefix. Empty = answer anyone.")),
                "whatsapp", "httpx", "https://developers.facebook.com/apps",
                ReloadMode.GATEWAY, lambda env: bool(env.get("WHATSAPP_TOKEN")), None),
    Integration("google_calendar", "Calendar & Productivity", "Google Calendar",
                "Lets Waku create and update Google Calendar events.",
                (EnvField("WAKU_GOOGLE_CALENDAR", "Enable Google Calendar", FieldKind.BOOL),
                 EnvField("WAKU_GOOGLE_CALENDAR_ID", "Calendar ID", default="primary")),
                "gcal", "googleapiclient", "https://developers.google.com/calendar/api/quickstart/python",
                ReloadMode.AGENT, lambda env: bool(env.get("WAKU_GOOGLE_CALENDAR")), None),
    Integration("apple_calendar", "Calendar & Productivity", "Apple Calendar",
                "Lets Waku work with Apple Calendar on this Mac.",
                (EnvField("WAKU_APPLE_CALENDAR", "Enable Apple Calendar", FieldKind.BOOL),
                 EnvField("WAKU_APPLE_CALENDARS", "Calendars")), None, None, "", ReloadMode.AGENT,
                _darwin, None),
    Integration("apple_tools", "Calendar & Productivity", "Apple Tools",
                "Lets Waku use Apple Mail and other local Apple tools.",
                (EnvField("WAKU_APPLE_TOOLS", "Enable Apple tools", FieldKind.BOOL),), None, None,
                "", ReloadMode.AGENT, _darwin_tools, None),
    Integration("notion", "Memory & Storage", "Notion", "Stores episodic memory in a Notion database.",
                (EnvField("WAKU_EPISODIC_STORE", "Episodic store", FieldKind.CHOICE,
                          default="sqlite", options=("sqlite", "notion")),
                 EnvField("NOTION_TOKEN", "Integration token", required=True, secret=True),
                 EnvField("NOTION_EPISODES_DATABASE_ID", "Episodes database ID", required=True)),
                "notion", "notion_client", "https://www.notion.so/my-integrations", ReloadMode.AGENT,
                lambda env: env.get("WAKU_EPISODIC_STORE") == "notion", None, _notion_normalize),
    Integration("supabase", "Memory & Storage", "Supabase", "Stores semantic memory in Supabase pgvector.",
                (EnvField("WAKU_SEMANTIC_STORE", "Semantic store", FieldKind.CHOICE,
                          default="sqlite", options=("sqlite", "supabase")),
                 EnvField("SUPABASE_URL", "Project URL", required=True),
                 EnvField("SUPABASE_SERVICE_KEY", "Service key", required=True, secret=True,
                          help="Embeddings also require OPENAI_API_KEY (optionally OPENAI_EMBED_MODEL).")),
                "supabase", "supabase", "", ReloadMode.AGENT,
                lambda env: env.get("WAKU_SEMANTIC_STORE") == "supabase", None),
    Integration("tavily", "Search & Observability", "Tavily", "Lets Waku search the web.",
                (EnvField("TAVILY_API_KEY", "API key", secret=True),), None, None,
                "https://tavily.com", ReloadMode.LIVE, lambda env: bool(env.get("TAVILY_API_KEY")), None),
    Integration("otel", "Search & Observability", "OpenTelemetry", "Exports traces to an OTLP collector.",
                (EnvField("OTEL_EXPORTER_OTLP_ENDPOINT", "OTLP endpoint"),), "tracing", "opentelemetry",
                "", ReloadMode.AGENT, lambda env: bool(env.get("OTEL_EXPORTER_OTLP_ENDPOINT")), None),
)


def _integration_from_provider(name: str, provider: Provider) -> Integration:
    fields = (EnvField(provider.key_env, "API Key", secret=True),)
    if provider.base_url_env and provider.endpoints:
        fields += (EnvField(provider.base_url_env, "Base URL", FieldKind.CHOICE,
                            default=provider.base_url or "",
                            options=tuple(endpoint.base_url for endpoint in provider.endpoints),
                            option_labels=tuple(endpoint.label for endpoint in provider.endpoints)),)
    return Integration(name, "AI Providers", name.replace("_", " ").title(),
                       f"Uses {name.replace('_', ' ').title()} models.",
                       fields, None, None, "",
                       ReloadMode.AGENT, lambda env, key=provider.key_env: bool(env.get(key)), _provider_probe)


def provider_integrations() -> tuple[Integration, ...]:
    return tuple(_integration_from_provider(name, provider) for name, provider in PROVIDERS.items())


def registry() -> tuple[Integration, ...]:
    return provider_integrations() + INTEGRATIONS


# T2 implementation: the cache path mirrors waku.ops.catalog's .waku storage.
_IMPORT_OK: dict[str, bool] = {}
_HEALTH: dict[str, IntegrationStatus] | None = None
_gateway_status_provider: Callable[[str], IntegrationStatus | None] | None = None
_gateway_reloader: Callable[[set[str]], dict[str, IntegrationStatus]] | None = None


def _health_path() -> Path:
    # Keep CLI and Dashboard aligned with the rest of Waku's runtime files.
    from waku.config import load_settings

    return load_settings().home / "connections_health.json"


def _reset_import_cache() -> None:
    _IMPORT_OK.clear()


def _extra_installed(import_name: str) -> bool:
    if import_name not in _IMPORT_OK:
        try:
            _IMPORT_OK[import_name] = importlib.util.find_spec(import_name) is not None
        except ValueError:
            # Test doubles and a few lazy optional packages can be present in
            # sys.modules without a spec; they are still importable in practice.
            _IMPORT_OK[import_name] = import_name in sys.modules
    return _IMPORT_OK[import_name]


def _health() -> dict[str, IntegrationStatus]:
    global _HEALTH
    if _HEALTH is not None:
        return _HEALTH
    _HEALTH = {}
    try:
        raw = json.loads(_health_path().read_text(encoding="utf-8"))
        for key, value in raw.items():
            _HEALTH[key] = IntegrationStatus(IntegrationState(value["state"]), value.get("message", ""),
                                             value.get("checked_at"))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return _HEALTH


def _save_health() -> None:
    path = _health_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({key: asdict(value) for key, value in _health().items()}), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass


def record_health(key: str, status: IntegrationStatus) -> None:
    checked_at = status.checked_at or datetime.now(UTC).isoformat()
    _health()[key] = IntegrationStatus(status.state, status.message, checked_at)
    _save_health()


def invalidate_health(key: str) -> None:
    _health().pop(key, None)
    _save_health()


def register_gateway_status_provider(fn: Callable[[str], IntegrationStatus | None]) -> None:
    global _gateway_status_provider
    _gateway_status_provider = fn


def register_gateway_reloader(fn: Callable[[set[str]], dict[str, IntegrationStatus]]) -> None:
    global _gateway_reloader
    _gateway_reloader = fn


def _configured(field: EnvField, value: str) -> bool:
    if field.kind is FieldKind.BOOL:
        return value not in ("", "0")
    return bool(value)


def _status(integration: Integration, env: Mapping[str, str]) -> IntegrationStatus:
    values = [env.get(field.name, "") for field in integration.env]
    enabled = integration.enabled(env)
    any_configured = any(_configured(field, value) for field, value in zip(integration.env, values, strict=True))
    if not enabled and not any_configured:
        message = "macOS only" if integration.key.startswith("apple_") and sys.platform != "darwin" else ""
        return IntegrationStatus(IntegrationState.NOT_CONFIGURED, message)
    missing = [field.name for field in integration.env if field.required and not env.get(field.name)]
    if missing:
        return IntegrationStatus(IntegrationState.INSTALLED_BUT_UNCONFIGURED, f"missing {', '.join(missing)}")
    if integration.import_name and not _extra_installed(integration.import_name):
        return IntegrationStatus(IntegrationState.INSTALLED_BUT_UNCONFIGURED, f"missing {integration.extra} extra")
    if integration.reload is ReloadMode.GATEWAY and _gateway_status_provider:
        status = _gateway_status_provider(integration.key)
        if status is not None:
            return status
    # Everything required is present and the extra is installed. Whether it
    # actually WORKS is a separate question, answered only by a probe — so the
    # honest answer when no probe has run is "configured", not "needs setup".
    return _health().get(integration.key, IntegrationStatus(IntegrationState.CONFIGURED,
                                                            "configured — not tested yet"))


def list_integrations() -> tuple[IntegrationView, ...]:
    env = os.environ
    views = []
    for integration in registry():
        fields = tuple(
            FieldView(field.name, field.label, field.kind, field.required, field.secret,
                      _configured(field, env.get(field.name, "")),
                      env.get(field.name, "")[-4:] if field.secret else "",
                      field.options, "" if field.secret else env.get(field.name, field.default), field.help,
                      field.option_labels)
            for field in integration.env
        )
        views.append(IntegrationView(integration.key, integration.group, integration.name, integration.what,
                                     _status(integration, env), fields,
                                     f"pip install -e '.[{integration.extra}]'" if integration.extra else "",
                                     integration.setup_url))
    return tuple(views)


def list_providers() -> tuple[IntegrationView, ...]:
    """AI Provider integrations only."""
    return tuple(view for view in list_integrations() if view.group == "AI Providers")


def list_connections() -> tuple[IntegrationView, ...]:
    """Non-provider integrations: channels, memory, search, observability, productivity."""
    return tuple(view for view in list_integrations() if view.group != "AI Providers")


def render_env_example_block() -> str:
    """Render the deterministic Connections section of ``.env.example``."""
    lines = [
        "# BEGIN GENERATED CONNECTIONS",
        "# Generated by scripts/generate_env_example.py; definitions live in waku/integrations.py",
        "",
        "WAKU_PROVIDER=anthropic",
    ]
    group = ""
    for integration in registry():
        if integration.group != group:
            group = integration.group
            lines.extend(("", f"# ── {group} ──"))
        lines.extend(("", f"# ── {integration.name} ──", f"# {integration.what}"))
        if integration.extra:
            lines.append(f"# Install: pip install -e '.[{integration.extra}]'")
        if integration.setup_url:
            lines.append(f"# Setup: {integration.setup_url}")
        for field in integration.env:
            if field.help:
                lines.append(f"# {field.help}")
            if field.kind is FieldKind.BOOL:
                lines.append("# Set to 1 to enable.")
            elif field.kind is FieldKind.CHOICE:
                lines.append(f"# Choices: {', '.join(field.options)}.")
            lines.append(f"# {field.name}={field.default}")
    lines.extend(("", "# END GENERATED CONNECTIONS", ""))
    return "\n".join(lines)


def cli_main() -> int:
    """Print the current Connections surface and return a meaningful exit code."""
    from rich.console import Console

    console = Console()
    group = ""
    failed = False
    for view in list_integrations():
        if view.group != group:
            group = view.group
            console.print(f"\n[bold]{group}[/bold]")
        status = view.status
        label = status.state.replace("_", " ")
        detail = status.message if status.state is not IntegrationState.NOT_CONFIGURED else ""
        checked = f" (checked {status.checked_at})" if status.checked_at else ""
        console.print(f"  {view.name:<20} {label}{' · ' + detail if detail else ''}{checked}")
        if status.state is IntegrationState.ERROR and any(field.configured for field in view.fields):
            failed = True
    return int(failed)


def _find_integration(key: str) -> Integration | None:
    return next((item for item in registry() if item.key == key), None)


def _env_path() -> Path:
    from dotenv import find_dotenv

    return Path(find_dotenv(usecwd=True) or ".env")


def _safe_error(exc: Exception, values: Mapping[str, str], integration: Integration) -> str:
    message = str(exc)
    for field in integration.env:
        if field.secret and (secret := values.get(field.name)):
            message = message.replace(secret, "***")
    return message or "connection test failed"


def _notion_probe(values: Mapping[str, str]) -> None:
    from waku.memory.episodic.notion_store import NotionEpisodeStore

    NotionEpisodeStore(values.get("NOTION_TOKEN"), values.get("NOTION_EPISODES_DATABASE_ID"))


def _google_calendar_probe(values: Mapping[str, str]) -> None:
    from waku.config import load_settings
    from waku.tools import calendar

    calendar.probe_google_calendar(
        load_settings().home,
        values.get("WAKU_GOOGLE_CALENDAR_ID", "") or "primary",
    )


def _apple_calendar_probe(values: Mapping[str, str]) -> None:
    from waku.tools import calendar

    calendar.probe_apple_calendar()


def _apple_tools_probe(values: Mapping[str, str]) -> None:
    from waku.tools import apple

    apple.probe_apple_tools()


def _tavily_probe(values: Mapping[str, str]) -> None:
    body = json.dumps({"api_key": values.get("TAVILY_API_KEY", ""), "query": "health check", "max_results": 1}).encode()
    request = urllib.request.Request("https://api.tavily.com/search", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed provider endpoint
        if response.status >= 300:
            raise ValueError(f"Tavily returned HTTP {response.status}")


def _otel_probe(values: Mapping[str, str]) -> None:
    """Check that the configured OTLP/gRPC collector accepts TCP connections."""
    endpoint = values.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OTLP endpoint must be a valid host:port") from exc
    if (not parsed.hostname or port is None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("OTLP endpoint must be host:port (for example localhost:4317)")
    connection = socket.create_connection((parsed.hostname, port), timeout=3)
    connection.close()


def _provider_probe(values: Mapping[str, str]) -> None:
    # Catalog has the provider-specific URL logic; a list operation is safe and
    # is deliberately the same non-writing check used by the existing UI.
    from waku.ops import catalog

    provider = next((name for name, item in PROVIDERS.items() if item.key_env in values), "")
    if not provider:
        return
    # A Save must validate the candidate key/endpoint, never a cached result
    # produced by a previous credential for the same URL.
    result = catalog.list_models(provider, use_cache=False)
    if result.get("error"):
        raise ValueError(result["error"])


def _probed(integration: Integration) -> Integration:
    if integration.key == "apple_calendar":
        return Integration(**{**integration.__dict__, "probe": _apple_calendar_probe})
    if integration.key == "apple_tools":
        return Integration(**{**integration.__dict__, "probe": _apple_tools_probe})
    if integration.key == "google_calendar":
        return Integration(**{**integration.__dict__, "probe": _google_calendar_probe})
    if integration.key == "notion":
        return Integration(**{**integration.__dict__, "probe": _notion_probe})
    if integration.key == "tavily":
        return Integration(**{**integration.__dict__, "probe": _tavily_probe})
    if integration.key == "otel":
        return Integration(**{**integration.__dict__, "probe": _otel_probe})
    return integration


def _current_view(key: str) -> IntegrationView | None:
    return next((view for view in list_integrations() if view.key == key), None)


def _write_updates(updates: Mapping[str, str], clear: tuple[str, ...]) -> None:
    from dotenv import set_key, unset_key

    path = _env_path()
    for name in clear:
        unset_key(str(path), name)
        os.environ.pop(name, None)
    for name, value in updates.items():
        set_key(str(path), name, value)
        os.environ[name] = value


def _restore(path: Path, contents: str | None, environment: Mapping[str, str | None]) -> None:
    if contents is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        path.write_text(contents, encoding="utf-8")
    for name, value in environment.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def apply_integration(key: str, values: Mapping[str, str], clear: tuple[str, ...] = (), *, force: bool = False) -> ApplyResult:
    integration = _find_integration(key)
    if integration is None:
        return ApplyResult(False, error="unknown integration")
    # Providers have cross-field switching semantics and therefore their own API.
    if integration.group == "AI Providers":
        return ApplyResult(False, error="use apply_provider for AI Providers")
    fields = {field.name: field for field in integration.env}
    if any(name not in fields for name in values) or any(name not in fields for name in clear):
        return ApplyResult(False, error="unknown configuration field")
    merged = {field.name: os.environ.get(field.name, field.default) for field in integration.env}
    updates: dict[str, str] = {}
    clears = tuple(clear)
    for name, raw in values.items():
        field = fields[name]
        value = str(raw)
        if field.secret and not value:
            continue
        if field.kind is FieldKind.BOOL:
            value = "1" if value.strip() and value.strip() != "0" else ""
        elif field.kind is FieldKind.CHOICE and value not in field.options:
            return ApplyResult(False, error=f"{name} must be one of: {', '.join(field.options)}")
        merged[name] = value
        updates[name] = value
    for name in clears:
        merged[name] = ""
        updates.pop(name, None)
    try:
        if integration.normalize:
            merged = integration.normalize(dict(merged))
            updates = {name: merged[name] for name in updates}
        enabled = integration.enabled(merged)
        missing = [field.name for field in integration.env if field.required and enabled and not merged.get(field.name)]
        if missing:
            return ApplyResult(False, error=f"missing {', '.join(missing)}")
        if enabled and integration.import_name and not _extra_installed(integration.import_name):
            return ApplyResult(False, error=f"missing {integration.extra} extra")
        tested = False
        effective = _probed(integration)
        if enabled and effective.probe and not force:
            effective.probe(merged)
            tested = True
    except Exception as exc:
        return ApplyResult(False, error=_safe_error(exc, merged, integration), can_force=True)
    changed = set(updates) | set(clears)
    path = _env_path()
    contents = path.read_text(encoding="utf-8") if path.exists() else None
    before = {name: os.environ.get(name) for name in changed}
    try:
        invalidate_health(key)
        _write_updates(updates, clears)
        if integration.reload is ReloadMode.AGENT:
            from waku.ops import browser_agent

            if error := browser_agent.rebuild():
                raise RuntimeError(error)
        elif integration.reload is ReloadMode.GATEWAY and _gateway_reloader:
            statuses = _gateway_reloader({key})
            status = statuses.get(key)
            if status and status.state is IntegrationState.ERROR:
                raise RuntimeError(status.message or "gateway failed to start")
            if status:
                record_health(key, status)
        if force:
            record_health(key, IntegrationStatus(IntegrationState.ERROR, "Saved without a successful test"))
        elif tested or integration.reload is ReloadMode.GATEWAY:
            record_health(key, IntegrationStatus(IntegrationState.CONNECTED))
    except Exception as exc:
        _restore(path, contents, before)
        record_health(key, IntegrationStatus(IntegrationState.ERROR, _safe_error(exc, merged, integration)))
        if integration.reload is ReloadMode.GATEWAY and _gateway_reloader:
            _gateway_reloader({key})
        return ApplyResult(False, error=_safe_error(exc, merged, integration))
    return ApplyResult(True, _current_view(key))


def test_integration(key: str) -> IntegrationView:
    integration = _find_integration(key)
    if integration is None:
        raise ValueError("unknown integration")
    values = {field.name: os.environ.get(field.name, field.default) for field in integration.env}
    if not integration.enabled(values):
        view = _current_view(key)
        assert view is not None
        # Switched off. The two "not ready" states survive as they are — turning
        # a toggle off doesn't fill in a missing token. Anything that WAS ready
        # (configured / connected / error) drops to CONFIGURED, because a
        # disabled integration must never keep claiming it is connected.
        state = (
            view.status.state
            if view.status.state in (IntegrationState.NOT_CONFIGURED,
                                     IntegrationState.INSTALLED_BUT_UNCONFIGURED)
            else IntegrationState.CONFIGURED
        )
        status = IntegrationStatus(state, "integration is disabled", view.status.checked_at)
        return IntegrationView(view.key, view.group, view.name, view.what, status, view.fields,
                               view.install_command, view.setup_url)
    effective = _probed(integration)
    if effective.probe is None:
        view = _current_view(key)
        assert view is not None
        status = IntegrationStatus(view.status.state, "no test available", view.status.checked_at)
        return IntegrationView(view.key, view.group, view.name, view.what, status, view.fields,
                               view.install_command, view.setup_url)
    try:
        effective.probe(values)
    except Exception as exc:
        record_health(key, IntegrationStatus(IntegrationState.ERROR, _safe_error(exc, values, integration)))
    else:
        record_health(key, IntegrationStatus(IntegrationState.CONNECTED))
    view = _current_view(key)
    assert view is not None
    return view


def apply_provider(provider: str, *, key: str | None = None, model: str | None = None,
                   small_model: str | None = None, base_url: str | None = None,
                   custom_key: str | None = None, force: bool = False,
                   activate: bool = True) -> ApplyResult:
    """Save provider fields and optionally make that provider active."""
    if provider not in PROVIDERS:
        return ApplyResult(False, error="unknown provider")
    from waku.ops import browser_agent, catalog

    previous = os.environ.get("WAKU_PROVIDER", "")
    selected = PROVIDERS[provider]
    switching = activate and provider != previous
    updates: dict[str, str] = {"WAKU_PROVIDER": provider} if activate else {}
    if model is not None:
        updates["WAKU_MODEL"] = model
    elif switching:
        updates["WAKU_MODEL"] = catalog.default_model_for(provider)
    if small_model is not None:
        updates["WAKU_SMALL_MODEL"] = small_model
    elif switching:
        updates["WAKU_SMALL_MODEL"] = ""
    if key:
        updates[selected.key_env] = key
    # Regional providers own their endpoint choice.  Keeping it beside that
    # provider's key prevents a MiniMax URL, for example, from leaking into Kimi
    # after a switch.  A legacy WAKU_BASE_URL for the current provider is
    # migrated the next time its endpoint is saved.
    if selected.base_url_env and (base_url is not None or switching):
        legacy = os.environ.get("WAKU_BASE_URL", "") if provider == previous else ""
        selected_base_url = (base_url or legacy or selected.configured_base_url() or "").strip()
        if selected_base_url:
            updates[selected.base_url_env] = selected_base_url
        updates["WAKU_BASE_URL"] = ""
    elif base_url is not None:
        updates["WAKU_BASE_URL"] = base_url
    if custom_key is not None:
        updates["WAKU_API_KEY"] = custom_key
    # The modal always submits its selected Base URL.  Compare effective values
    # rather than field presence so reopening and saving an unchanged provider
    # does not perform a synchronous network probe every time.
    current_key = os.environ.get(selected.key_env, "")
    legacy_base_url = os.environ.get("WAKU_BASE_URL", "") if provider == previous else ""
    current_base_url = legacy_base_url or selected.configured_base_url() or ""
    candidate_base_url = (
        updates.get(selected.base_url_env, current_base_url)
        if selected.base_url_env else updates.get("WAKU_BASE_URL", current_base_url)
    )
    key_changed = bool(key and key != current_key)
    base_url_changed = (
        base_url is not None
        and candidate_base_url.rstrip("/") != current_base_url.rstrip("/")
    )
    changed_updates = {
        name: value for name, value in updates.items()
        if (os.environ.get(name) or "") != value
    }
    path = _env_path()
    contents = path.read_text(encoding="utf-8") if path.exists() else None
    before = {name: os.environ.get(name) for name in changed_updates}
    try:
        # Validate a newly supplied key before persisting it.  catalog's probe
        # intentionally reads environment variables, so expose only the
        # candidate values for the duration of this non-writing request.
        candidate_key = key or os.environ.get(selected.key_env, "")
        if candidate_key and (key_changed or base_url_changed) and not force:
            probe_names = {"WAKU_PROVIDER", selected.key_env}
            if selected.base_url_env:
                probe_names.update({selected.base_url_env, "WAKU_BASE_URL"})
            probe_before = {name: os.environ.get(name) for name in probe_names}
            os.environ["WAKU_PROVIDER"] = provider
            os.environ[selected.key_env] = candidate_key
            if selected.base_url_env and selected.base_url_env in updates:
                os.environ[selected.base_url_env] = updates[selected.base_url_env]
                os.environ["WAKU_BASE_URL"] = ""
            elif base_url is not None:
                os.environ["WAKU_BASE_URL"] = base_url
            try:
                _provider_probe({selected.key_env: candidate_key})
            finally:
                for name, old in probe_before.items():
                    if old is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = old
        if changed_updates:
            _write_updates(changed_updates, ())
        affects_active_agent = bool(changed_updates) and (provider == previous or activate)
        if affects_active_agent and browser_agent.current() is not None:
            if error := browser_agent.rebuild():
                raise RuntimeError(error)
            current_agent = browser_agent.current()
            if current_agent is not None:
                current_agent.tracer.event("config", {"from": {"provider": previous},
                                                       "to": {"provider": provider}})
        status = (IntegrationStatus(IntegrationState.ERROR, "Saved without a successful test")
                  if force else IntegrationStatus(IntegrationState.CONNECTED))
        record_health(provider, status)
    except Exception as exc:
        _restore(path, contents, before)
        result = _safe_error(exc, changed_updates, _find_integration(provider) or provider_integrations()[0])
        return ApplyResult(False, error=result, can_force=bool(key or os.environ.get(selected.key_env)))
    return ApplyResult(True, _current_view(provider))


def apply_provider_disabled(provider: str, *, disabled: bool) -> ApplyResult:
    """Toggle a provider's availability (the Models grid's enable/disable).

    Disabled providers are hidden from pickers/switchers but keep their key.
    The ACTIVE provider can't be disabled — switch to another provider first.
    """
    if provider not in PROVIDERS:
        return ApplyResult(False, error="unknown provider")
    if disabled and os.environ.get("WAKU_PROVIDER", "") == provider:
        return ApplyResult(False, error="cannot disable the current provider — switch to another provider first")
    current = {p.strip() for p in os.environ.get("WAKU_DISABLED_PROVIDERS", "").split(",") if p.strip()}
    updated = (current | {provider}) if disabled else (current - {provider})
    _write_updates({"WAKU_DISABLED_PROVIDERS": ",".join(sorted(updated))}, ())
    return ApplyResult(True, _current_view(provider))
