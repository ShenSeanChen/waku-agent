"""Model access — eight providers, one loop, zero framework.

The loop speaks one dialect: Anthropic's Messages shape (system/messages/tools
in, content blocks out). Providers plug in two ways:

  anthropic wire format (native)     → Anthropic, Kimi/Moonshot, GLM/Z.ai, MiniMax
  openai wire format (thin adapter)  → OpenAI, Google Gemini, DeepSeek, OpenRouter

Pick with WAKU_PROVIDER=anthropic|openai|gemini|deepseek|minimax|kimi|glm|openrouter
and set that provider's API key in .env. Override the model ids with WAKU_MODEL /
WAKU_SMALL_MODEL if the defaults below age out — they're just strings. This
matters most for openrouter: it's a single key in front of hundreds of models,
so WAKU_MODEL=<vendor>/<model> (e.g. "google/gemini-3.5-flash") picks whichever
one you want — and its defaults below are $0 ":free" ids, so it works with no
spend at all (rate-limited). The dashboard Settings tab lists the live catalog.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from waku.config import Settings


@dataclass(frozen=True)
class ProviderEndpoint:
    """One official regional endpoint and its matching model catalog."""

    label: str
    base_url: str
    catalog_url: str | None = None


@dataclass(frozen=True)
class Provider:
    kind: str        # 'anthropic' or 'openai' — the wire format
    key_env: str     # which env var holds the key
    base_url: str | None
    model: str       # default main model (the loop)
    small_model: str  # default cheap model (retrieval gate + consolidation)
    # Where to LIST this provider's models (the Settings picker). openai-wire
    # providers get {base_url}/models automatically; set this for providers
    # whose chat endpoint and catalog endpoint differ (e.g. kimi talks the
    # anthropic wire but lists models on its OpenAI-compatible API). The
    # defaults above are just starting points — any listed model is one click.
    catalog_url: str | None = None
    # The two models the chat switcher pins by default for this provider: a
    # flagship (top quality) and a fast one (cheap/low-latency). Distinct from
    # model/small_model — e.g. anthropic's loop default is sonnet-5, but the
    # flagship you'd showcase is opus-4.8. Blank falls back to model/small_model.
    flagship: str = ""
    fast: str = ""
    # Providers with separately-issued regional keys keep their endpoint choice
    # in a provider-specific env var.  WAKU_BASE_URL remains the global custom
    # override for backwards compatibility, but must not leak across providers.
    base_url_env: str = ""
    endpoints: tuple[ProviderEndpoint, ...] = ()
    # Seven opt-in fields, used only by the hosted free-tier row. Each
    # defaults to the behaviour every other row already has.
    label: str = ""
    hidden_unless_env: bool = False
    claims_families: bool = True
    catalog_from_base_url: bool = False
    model_env: str = ""
    small_model_env: str = ""
    scoped_credentials: bool = False

    def label_text(self, name: str) -> str:
        """What the UI calls this row."""
        return self.label or name.replace("_", " ").title()

    def models_now(self) -> tuple[str, str]:
        """(model, small_model) with the environment overrides applied.

        Read at call time, never at import: PROVIDERS is built once when the
        module loads, and a tenant container sets these after that. Every
        reader of a row's models goes through here so none can miss it.
        """
        model = os.getenv(self.model_env, "").strip() if self.model_env else ""
        small = os.getenv(self.small_model_env, "").strip() if self.small_model_env else ""
        return (model or self.model, small or self.small_model)

    def is_visible(self) -> bool:
        """False while a hidden row's endpoint is unset -- a local user must
        never see the hosted row in a page, a list or an error message."""
        if not self.hidden_unless_env:
            return True
        return bool(os.getenv(self.base_url_env, "").strip()) if self.base_url_env else False

    def default_pair(self) -> list[str]:
        """[flagship, fast], deduped — the switcher's default picks."""
        pair = [self.flagship or self.model, self.fast or self.small_model]
        return list(dict.fromkeys(m for m in pair if m))

    def configured_base_url(self) -> str | None:
        """Provider-scoped endpoint, falling back to the built-in default."""
        configured = os.getenv(self.base_url_env, "").strip() if self.base_url_env else ""
        return configured or self.base_url

    def catalog_for(self, base_url: str | None) -> str | None:
        """Return the catalog paired with *base_url*'s region."""
        normalized = (base_url or "").rstrip("/")
        for endpoint in self.endpoints:
            if endpoint.base_url.rstrip("/") == normalized:
                return endpoint.catalog_url
        return self.catalog_url


def _registry() -> dict:
    """Read waku/providers.toml -- the whole provider list, as data.

    A provider used to be a row here plus a pricing row, a key-url row, an
    .env.example block and two to four separate eval files: seven to nine files
    for what the rulebook calls a one-line change. It is now one table in that
    file and a logo, and everything below is built from it, so a provider pull
    request is a diff a maintainer can read at a glance.
    """
    path = Path(__file__).resolve().parents[1] / "providers.toml"
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _provider(row: dict) -> Provider:
    return Provider(
        kind=row["kind"], key_env=row["key_env"], base_url=row.get("base_url"),
        model=row["model"], small_model=row["small_model"],
        catalog_url=row.get("catalog_url"),
        flagship=row.get("flagship", ""), fast=row.get("fast", ""),
        base_url_env=row.get("base_url_env", ""),
        endpoints=tuple(ProviderEndpoint(e["label"], e["base_url"], e.get("catalog_url"))
                        for e in row.get("endpoints", ())),
        label=row.get("label", ""),
        hidden_unless_env=row.get("hidden_unless_env", False),
        claims_families=row.get("claims_families", True),
        catalog_from_base_url=row.get("catalog_from_base_url", False),
        model_env=row.get("model_env", ""),
        small_model_env=row.get("small_model_env", ""),
        scoped_credentials=row.get("scoped_credentials", False),
    )


REGISTRY: dict[str, dict] = _registry()
PROVIDERS: dict[str, Provider] = {name: _provider(row) for name, row in REGISTRY.items()}

# Where each provider's key actually comes from. Pointing at ".env.example"
# was useless advice for anyone who installed from PyPI — that file only exists
# in a git checkout, so the one instruction the message gave could not be
# followed by the people most likely to need it.
KEY_URLS: dict[str, str] = {name: row["key_url"] for name, row in REGISTRY.items()}


def _visible_names() -> list[str]:
    """Provider names a local user is allowed to see: every row, minus a
    hidden_unless_env row whose endpoint isn't set. Read at call time (never
    cached) because is_visible() depends on the environment, which a tenant
    container sets after PROVIDERS is built."""
    return sorted(name for name, provider in PROVIDERS.items() if provider.is_visible())


def _no_key_message(name: str, key_env: str) -> str:
    """Say what to set, where to get it, and WHICH file we read.

    The old message named one env var and pointed at a file that does not exist
    off a git checkout. Three things were missing and each one cost a search:
    the URL to get a key, the absolute path of the .env actually in play, and
    the fact that Waku speaks to eleven providers, not one.
    """
    from waku.config import DOTENV_PATH

    # Name the variable in BOTH branches. "add the line there" without saying
    # which line is the same dead end as pointing at .env.example was.
    where = (f"Add it to {DOTENV_PATH}:\n"
             f"    {key_env}=your-key-here"
             if DOTENV_PATH else
             f"No .env found from {os.getcwd()} upward — create one here:\n"
             f"    echo '{key_env}=your-key-here' >> .env")
    url = KEY_URLS.get(name)
    return (
        f"No API key for provider '{name}'.\n\n"
        f"  1. Get a key: {url}\n" if url else f"No API key for provider '{name}'.\n\n"
    ) + (
        f"  2. {where}\n\n"
        f"Other providers: {', '.join(_visible_names())}\n"
        f"Switch with WAKU_PROVIDER=<name> and that provider's key."
    )


def _families(provider: Provider) -> set[str]:
    """The model FAMILIES this provider ships — "claude", "grok", "gemini"...

    Taken from the provider's own four defaults rather than a hand-kept list, so
    a new provider is covered the moment it is added. Aggregators whose ids are
    vendor-namespaced ("anthropic/claude-3" on openrouter) are excluded by the
    caller: for them the first segment names a VENDOR, not the host.
    """
    names = (provider.model, provider.small_model, provider.flagship, provider.fast)
    return {n.split("-")[0].lower() for n in names if n and "/" not in n}


def _belongs_elsewhere(model: str, provider_name: str) -> bool:
    """Is this model name positively another provider's?

    Deliberately asks the POSITIVE question. "Does it not look like ours?" would
    drop anything unfamiliar — a legitimately odd id, a preview name, a model
    added since — and silently downgrade a deliberate choice. This only fires
    when the family is one some OTHER provider actually owns, which is the case
    that produces a 400 rather than a surprise.
    """
    family = model.split("-")[0].lower()
    if "/" in model or not family:
        return False
    owner = {f: name for name, p in PROVIDERS.items()
             if p.claims_families and "/" not in (p.model or "x")
             for f in _families(p)}.get(family)
    return bool(owner) and owner != provider_name


def get_client(settings: Settings):
    """Build the client for settings.provider and fill in default model ids.
    Returns anything with .messages.create(...) in the Anthropic shape."""
    provider = PROVIDERS.get(settings.provider)
    if provider is None:
        raise SystemExit(f"Unknown WAKU_PROVIDER '{settings.provider}'. "
                         f"Pick one of: {', '.join(_visible_names())}")

    # A hidden row that isn't configured yet must never build a client — it has
    # no endpoint to talk to, and the message needs to name the variable that
    # would turn it on, not "no API key", which would be misleading here.
    if not provider.is_visible():
        raise SystemExit(
            f"'{settings.provider}' is not configured: set {provider.base_url_env} "
            f"to enable it."
        )

    # .strip() so a trailing newline/space from a copy-paste doesn't corrupt the
    # auth header (headers are latin-1; a stray non-ASCII char errors cryptically).
    # A scoped_credentials row (the hosted free tier) never falls back to
    # WAKU_API_KEY: that global override exists for BYOK and must not outrank
    # the platform token a tenant container was actually given.
    api_key = (os.getenv(provider.key_env, "") if provider.scoped_credentials
              else (settings.api_key or os.getenv(provider.key_env, ""))).strip()
    if not api_key:
        raise SystemExit(_no_key_message(settings.provider, provider.key_env))
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        raise SystemExit(
            f"{provider.key_env} contains a non-ASCII character (e.g. a smart quote "
            f"or arrow from a bad paste). Re-paste the key with no spaces or line breaks."
        )

    # A model name belongs to the provider it was configured FOR. WAKU_MODEL and
    # WAKU_SMALL_MODEL are global, so code that switches provider — the arena
    # races ten of them — carried anthropic's gate model to xAI, which answers
    # `400 Model not found: claude-haiku-4-5-20251001`. The retrieval gate then
    # FAILS OPEN by design, so it retrieved on every single turn for every
    # non-anthropic model instead of deciding, and reported that as a normal
    # "retrieve". A silent permanent failure wearing the costume of a healthy
    # decision.
    #
    # So: a value INHERITED from the env for a different provider is dropped
    # (the provider's own default fills in below); a value the caller passed
    # explicitly is kept, because that is a choice, not a leak. The two are
    # distinguishable exactly when the setting still equals the env string.
    for attr in ("model", "small_model"):
        inherited = os.getenv(f"WAKU_{attr.upper()}", "").strip()
        if inherited and getattr(settings, attr) == inherited \
                and _belongs_elsewhere(inherited, settings.provider):
            setattr(settings, attr, "")

    default_model, default_small_model = provider.models_now()
    settings.model = settings.model or default_model
    settings.small_model = settings.small_model or default_small_model
    # Same scoping as the key above: WAKU_BASE_URL is a global BYOK override
    # and must not leak into a scoped_credentials row's own endpoint.
    base_url = (provider.configured_base_url() if provider.scoped_credentials
               else settings.base_url or provider.configured_base_url())

    # a hung network call must never freeze a turn silently
    timeout = float(os.getenv("WAKU_LLM_TIMEOUT", "120"))

    if provider.kind == "anthropic":
        import anthropic

        kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        return anthropic.Anthropic(**kwargs)
    return OpenAICompatClient(api_key=api_key, base_url=base_url, timeout=timeout)


class OpenAICompatClient:
    """Speaks the Anthropic Messages shape the loop expects, backed by an
    OpenAI-style chat.completions API. ~60 lines is the entire difference
    between the two wire formats — worth reading once.
    """

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 120.0):
        import openai

        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _to_openai(self, *, model, messages, max_tokens, system=None, tools=None) -> dict:
        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                oai_messages.append({"role": message["role"], "content": content})
            elif message["role"] == "assistant":
                # anthropic content blocks → assistant text + tool_calls
                text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
                calls = []
                for b in content:
                    if getattr(b, "type", "") != "tool_use":
                        continue
                    call = {"id": b.id, "type": "function",
                            "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                    extra = getattr(b, "extra", None)   # Gemini thought_signature
                    if extra:
                        call["extra_content"] = extra
                    calls.append(call)
                entry: dict = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                oai_messages.append(entry)
            else:
                # anthropic tool_result blocks → one 'tool' message each
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        })

        kwargs: dict = {"model": model, "messages": oai_messages,
                        "max_completion_tokens": max_tokens}
        if tools:
            kwargs["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in tools
            ]
        return kwargs

    def _call(self, kwargs: dict, **extra):
        """Run chat.completions.create with the max_tokens key-name fallback
        (older OpenAI-compatible endpoints only know max_tokens, not the newer
        max_completion_tokens). Only retry when the error is ABOUT that param —
        retrying on any error masked the real failure (e.g. a gpt-5.x call would
        fail for some other reason, then the max_tokens retry buried it under a
        confusing 'use max_completion_tokens' message)."""
        try:
            return self._client.chat.completions.create(**kwargs, **extra)
        except Exception as exc:
            m = str(exc).lower()
            if "max_completion_tokens" not in m and "max_tokens" not in m:
                raise
            k = dict(kwargs)
            k["max_tokens"] = k.pop("max_completion_tokens", None)
            return self._client.chat.completions.create(**k, **extra)

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        response = self._call(self._to_openai(
            model=model, messages=messages, max_tokens=max_tokens, system=system, tools=tools))
        if not getattr(response, "choices", None):
            # some OpenAI-compatible endpoints (e.g. OpenRouter on a rate
            # limit) return 200 with an error body and no choices: surface
            # that message instead of dying on a TypeError below
            err = getattr(response, "error", None) or "endpoint returned no choices"
            raise RuntimeError(f"{model}: {err}")
        choice = response.choices[0].message
        blocks = []
        if choice.content:
            blocks.append(SimpleNamespace(type="text", text=choice.content))
        for call in choice.tool_calls or []:
            blocks.append(SimpleNamespace(
                type="tool_use", id=call.id, name=call.function.name,
                input=json.loads(call.function.arguments or "{}"),
                # Gemini's thinking models attach a thought_signature here and
                # REQUIRE it echoed back with the tool call next turn, else the
                # follow-up 400s ("missing a thought_signature"). Carry it so
                # _to_openai can put it back. None for every other provider.
                extra=getattr(call, "extra_content", None),
            ))
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            stop_reason="tool_use" if choice.tool_calls else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            ),
            content=blocks,
        )

    def _stream(self, *, model, messages, max_tokens, system=None, tools=None):
        """Anthropic-shaped streaming over an OpenAI chat.completions stream —
        same two-format bridge as _create, but yielding text as it arrives.
        Used by the loop when stream=True (e.g. the dashboard's live chat)."""
        kwargs = self._to_openai(
            model=model, messages=messages, max_tokens=max_tokens, system=system, tools=tools)
        return _OpenAIStream(self, kwargs)


class _OpenAIStream:
    """A context manager mirroring anthropic's messages.stream(): iterate
    .text_stream for text deltas, then .get_final_message() for the assembled
    Anthropic-shaped response (text + reassembled tool calls + usage)."""

    def __init__(self, client: OpenAICompatClient, kwargs: dict):
        self._client = client
        self._kwargs = kwargs
        self._text: list[str] = []
        self._tools: dict[int, dict] = {}   # index → {id, name, args}
        self._usage = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        stream = self._client._call(
            self._kwargs, stream=True, stream_options={"include_usage": True})
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self._usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                self._text.append(delta.content)
                yield delta.content
            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = self._tools.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

    def get_final_message(self):
        blocks = []
        text = "".join(self._text)
        if text:
            blocks.append(SimpleNamespace(type="text", text=text))
        for slot in self._tools.values():
            blocks.append(SimpleNamespace(
                type="tool_use", id=slot["id"], name=slot["name"],
                input=json.loads(slot["args"] or "{}")))
        usage = self._usage
        return SimpleNamespace(
            stop_reason="tool_use" if self._tools else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0)),
            content=blocks,
        )
