"""The model catalog — what you can actually run, and the shortlist you curated.

Two related jobs, both feeding the settings model picker:

1. **What exists.** `list_models()` asks a provider what it can serve. There is
   no single way to ask: some endpoints publish an explicit catalog URL (kimi
   chats on the Anthropic wire but lists on its OpenAI-compatible one), most
   OpenAI-compatible endpoints answer `GET {base_url}/models`, and some — the
   Anthropic wire among them — have no listing at all, so we fall back to that
   provider's own known defaults. Cached 5 minutes; failures are cached ~1
   minute WITH the reason, so an unreachable catalog can't stall the
   dashboard's 5-second poll and still tells you why.

2. **What you chose.** `.waku/models.json` holds an ordered `provider:model`
   shortlist. The chat switcher shows exactly these — the built-in defaults are
   a starting point, never the menu. The first pinned model for a provider is
   that provider's default when you switch to it.

Writing the shortlist lives here (`save_pinned`); the pin/unpin HTTP action
lives in settings_api, because its reply is a whole settings payload. That
keeps the dependency pointing one way: settings_api -> catalog, never back.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from waku.config import load_settings
from waku.ops.pricing import remember_price

_models_cache: dict[str, tuple[float, list]] = {}


def _opencode_models(data: dict) -> list[dict]:
    """Read OpenCode's /provider response without exposing credentials/options.

    Only connected providers are offered. Their catalog describes access and
    pricing; a listing does not guarantee capacity for the next request.
    """
    if (not isinstance(data, dict) or not isinstance(data.get("all"), list)
            or not isinstance(data.get("connected"), list)):
        raise TypeError("OpenCode /provider must return all and connected arrays")
    connected = data["connected"]
    models = []
    for provider in data["all"]:
        if not isinstance(provider, dict) or provider.get("id") not in connected:
            continue
        provider_id = provider.get("id")
        catalog = provider.get("models")
        if not isinstance(provider_id, str) or not isinstance(catalog, dict):
            continue
        for model_id, model in catalog.items():
            if not isinstance(model, dict):
                continue
            model_id = model.get("id") or model_id
            if not isinstance(model_id, str) or not model_id:
                continue
            mid = (model_id if provider_id == "opencode" and "/" not in model_id
                   else f"{provider_id}/{model_id}")
            capabilities = model.get("capabilities") or {}
            limits = model.get("limit") or {}
            entry = {
                "id": mid,
                "free": mid == "muse-spark-1.3-contributor-free",
                "tools": capabilities.get("toolcall"),
                "reasoning": capabilities.get("reasoning"),
                "context": limits.get("context"),
            }
            try:
                # OpenCode reports dollars per million tokens, unlike
                # OpenRouter's per-token strings. Scope rates to this provider.
                cost = model.get("cost") or {}
                pin, pout = float(cost["input"]), float(cost["output"])
                if not all(math.isfinite(p) and p >= 0 for p in (pin, pout)):
                    raise ValueError("invalid model price")
                remember_price(f"opencode_local:{mid}", pin, pout)
                entry.update(price_in=pin, price_out=pout, free=pin == pout == 0)
            except (KeyError, TypeError, ValueError):
                pass
            models.append(entry)
    return models


def _known_default_ids(prov, out: dict, is_active: bool) -> list[dict]:
    """Best-effort model list when the live catalog is unreachable: the provider's
    flagship + fast + loop/gate defaults — so the showcase model (e.g. opus-4.8)
    is offered too, not just the two loop defaults — plus the active model when
    this is the active provider."""
    ids = [*(prov.default_pair() if prov else []),
           prov.model if prov else "", prov.small_model if prov else ""]
    if is_active:
        ids = [out.get("model"), out.get("small_model"), *ids]
    return [{"id": m} for m in dict.fromkeys(m for m in ids if m)]


def list_models(provider: str | None = None, *, use_cache: bool = True) -> dict:
    """Model ids available on a provider, for the settings model picker — the
    defaults are starting points, never the menu. Pass `provider` to list ANY
    provider's catalog (the "Your models" add-row picks a provider first);
    without it, the ACTIVE provider is used. Sources: an explicit
    Provider.catalog_url (anthropic, kimi), GET {base_url}/models on
    OpenAI-compatible endpoints (OpenRouter, Gemini, any WAKU_BASE_URL), or the
    two known defaults when no catalog exists. OpenRouter entries carry free /
    tool-support / context metadata so the picker can surface the $0
    tool-capable models. Local OpenCode uses GET {base_url}/provider and is
    contacted only when selected or explicitly requested here. Cached 5 minutes."""
    import time
    import urllib.request

    from waku.loop.models import PROVIDERS

    s = load_settings()
    # An explicit provider overrides the active one (and its custom base_url:
    # WAKU_BASE_URL only applies to the provider it was set for).
    name = provider or s.provider
    prov = PROVIDERS.get(name)
    base = ((s.base_url if name == s.provider else None)
            or (prov.configured_base_url() if prov else None))
    out = {
        "provider": name,
        "model": (s.model if name == s.provider else "") or (prov.model if prov else ""),
        "small_model": ((s.small_model if name == s.provider else "")
                        or (prov.small_model if prov else "")),
        "endpoint": base or name,
    }
    # Where can this provider's models be listed? An explicit catalog_url wins
    # (kimi chats on the anthropic wire but lists on its OpenAI-compatible API;
    # anthropic itself has GET /v1/models); otherwise openai-wire endpoints get
    # {base_url}/models; otherwise fall back to the two known defaults.
    catalog_url = prov.catalog_for(base) if prov is not None else None
    if prov is not None and prov.kind == "opencode_local" and base:
        url = base.rstrip("/") + "/provider"
    elif catalog_url:
        url = catalog_url
    elif prov is not None and prov.kind == "openai" and base:
        url = base.rstrip("/") + "/models"
    else:
        # No catalog endpoint: fall back to the provider's own known defaults
        # (flagship + fast + loop/gate), not just the active model.
        return {**out, "listed": False,
                "models": _known_default_ids(prov, out, name == s.provider)}

    cached = _models_cache.get(url) if use_cache else None
    if cached and time.time() - cached[0] < 300:
        _ts, cmodels, cerr = cached          # cerr None on a real listing
        r = {**out, "listed": cerr is None, "models": cmodels}
        if cerr:
            r["error"] = cerr
        return r
    # Use this provider's own key; s.api_key only holds the ACTIVE provider's.
    key = (((s.api_key if name == s.provider else "") or os.getenv(prov.key_env, "")).strip()
           if prov.key_env else "")
    # HTTP headers must be latin-1; a key with a stray non-ASCII char (a smart
    # arrow/quote or a line-break from a bad paste) would otherwise crash the
    # whole listing with an opaque codec error and silently drop back to two
    # defaults. Catch it here with a message that actually says how to fix it.
    try:
        key.encode("latin-1")
    except UnicodeEncodeError:
        msg = (f"{prov.key_env} contains a non-ASCII character — re-paste the key "
               f"(no spaces, line breaks, or arrows).")
        return {**out, "listed": False,
                "models": _known_default_ids(prov, out, name == s.provider), "error": msg}
    # send both auth styles — Bearer for OpenAI-compatible catalogs, x-api-key +
    # version for Anthropic's; each server reads the header it knows.
    # Set a browser-like User-Agent: some OpenAI-compatible proxies (e.g.
    # opencode.ai) block Python-urllib/3.x with a 403 / error code 1010.
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Waku)"}
    if prov.key_env:
        headers.update({"Authorization": f"Bearer {key}", "x-api-key": key,
                        "anthropic-version": "2023-06-01"})
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if prov.kind == "opencode_local":
            local_models = _opencode_models(data)
    except Exception as exc:
        # Surface the server's actual reason (e.g. xAI's 403 "no credits"), not
        # just "HTTP Error 403" — an HTTPError carries the body on .read().
        msg = str(exc)
        try:
            msg = f"{msg} — {exc.read().decode()[:160]}"
        except Exception:
            pass
        # still offer the provider's known defaults so the picker isn't empty
        known = _known_default_ids(prov, out, name == s.provider)
        # cache the failure (defaults + reason) for ~1 minute so an unreachable
        # catalog doesn't stall every 5-second dashboard poll for 10s — and so a
        # cache hit still shows the defaults and the reason, not a blank list.
        _models_cache[url] = (time.time() - 240, known, msg)
        return {**out, "listed": False, "models": known, "error": msg}
    if prov.kind == "opencode_local":
        local_models.sort(key=lambda x: (not x["free"], x["tools"] is False, x["id"]))
        _models_cache[url] = (time.time(), local_models, None)
        return {**out, "listed": True, "models": local_models}
    models = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        params = m.get("supported_parameters")
        entry = {
            "id": mid,
            "free": mid.endswith(":free") or pricing.get("prompt") == "0",
            # None means the endpoint doesn't say (only OpenRouter reports this)
            "tools": ("tools" in params) if params is not None else None,
            # reasoning models spend tokens thinking out loud, which breaks the
            # gate's tiny budget: the UI steers them away from the gate slot
            "reasoning": ("reasoning" in params) if params is not None else None,
            "context": m.get("context_length"),
        }
        try:
            # OpenRouter prices are $/token strings; keep $/M for display + cost
            pin, pout = float(pricing["prompt"]) * 1e6, float(pricing["completion"]) * 1e6
            remember_price(mid, pin, pout)
            entry["price_in"], entry["price_out"] = round(pin, 3), round(pout, 3)
        except (KeyError, TypeError, ValueError):
            pass
        models.append(entry)
    models.sort(key=lambda x: (not x["free"], x["tools"] is False, x["id"]))
    _models_cache[url] = (time.time(), models, None)   # None error = a real listing
    return {**out, "listed": True, "models": models}


def _models_json() -> Path:
    return load_settings().home / "models.json"


def default_pinned_specs() -> list[str]:
    """Starter shortlist before the user has curated their own: flagship + fast
    for providers with a key, plus a selected/configured keyless provider.
    This reads configuration only; it never scans for local servers.
    Flagship comes first, so it's that provider's default."""
    from waku.loop.models import PROVIDERS

    specs = []
    for name, prov in PROVIDERS.items():
        configured = (bool(os.getenv(prov.key_env)) if prov.key_env else
                      os.getenv("WAKU_PROVIDER") == name
                      or bool(prov.base_url_env and os.getenv(prov.base_url_env)))
        if configured:
            specs += [f"{name}:{m}" for m in prov.default_pair()]
    return specs


def pinned_specs() -> list[str]:
    """The user's curated 'provider:model' shortlist (ordered), from
    .waku/models.json. The chat switcher shows exactly these. Before they've
    saved anything, fall back to the flagship+fast defaults."""
    p = _models_json()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("pinned", [])
        except (json.JSONDecodeError, OSError):
            pass
    return default_pinned_specs()


def default_model_for(provider: str) -> str:
    """A provider's default model = the FIRST one the user pinned for it.
    Empty string means 'use the provider's built-in default'."""
    for spec in pinned_specs():
        p, _, m = spec.partition(":")
        if p == provider and m:
            return m
    return ""


def save_pinned(specs: list[str]) -> None:
    """Persist the curated shortlist, in order. The ONLY writer of models.json —
    keep it that way so the file has one shape and one owner."""
    path = _models_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pinned": specs}, indent=1))
