"""Settings — read the current brain, or swap it, without restarting anything.

`settings_info()` is the read side: which provider and model are live, which
keys are set (masked to last-4 — the full key is never sent to the browser),
your pinned shortlist, and which optional backends are wired up. The whole
Settings tab is one call to this.

`apply_settings()` is the write side, and it is the sharp one. It writes to
`.env` AND `os.environ`, then rebuilds the shared agent so the switch is live
on the next message rather than the next restart. Two rules keep it safe:

  * only WHITELISTED env names are writable — a payload cannot set arbitrary
    environment variables just because the browser asked nicely;
  * a failed rebuild keeps the OLD agent (see browser_agent.rebuild), so a
    typo'd key doesn't leave you with no agent at all.

`pin_action` lives here rather than in catalog because its reply is a whole
settings payload; catalog owns the storage (`save_pinned`), this owns the HTTP
action. Dependencies point one way: settings_api -> catalog, never back.
"""

from __future__ import annotations

import os
import shutil

from waku.config import load_settings
from waku.ops import browser_agent, catalog


def pin_action(payload: dict) -> dict:
    """Manage the curated model shortlist: pin / unpin / make-default."""
    action = payload.get("action")
    provider, model = payload.get("provider", ""), payload.get("model", "")
    if not provider or not model:
        return {"error": "provider and model required"}
    spec = f"{provider}:{model}"
    specs = [s for s in catalog.pinned_specs() if s != spec]
    if action == "pin":
        specs.append(spec)
    elif action == "default":
        # move to the front of its provider's group -> becomes that provider's default
        idx = next((i for i, s in enumerate(specs) if s.split(":", 1)[0] == provider), len(specs))
        specs.insert(idx, spec)
    elif action != "unpin":
        return {"error": f"unknown action {action}"}
    catalog.save_pinned(specs)
    return {"ok": True, **settings_info()}


def settings_info() -> dict:
    """Current provider/model + which keys are set — masked to last-4, never
    the full key. `pinned` is the user's curated model shortlist (the chat
    switcher shows exactly these, across providers)."""
    from waku.loop.models import PROVIDERS

    s = load_settings()
    prov = PROVIDERS.get(s.provider)
    # the curated shortlist, in order; the first pinned model per provider is
    # that provider's default (used when you switch providers).
    pinned, seen = [], set()
    for spec in catalog.pinned_specs():
        p, _, m = spec.partition(":")
        if m:
            pinned.append({"provider": p, "model": m, "default": p not in seen})
            seen.add(p)
    # Group by provider for display (so all of one lab's models sit together,
    # e.g. a late-added claude-fable-5 joins the other anthropic rows). A STABLE
    # sort by provider's first-appearance order keeps each provider's own order —
    # so its default (first pinned) stays on top and the 'default' flags above
    # still line up.
    prov_order: dict = {}
    for row in pinned:
        prov_order.setdefault(row["provider"], len(prov_order))
    pinned.sort(key=lambda row: prov_order[row["provider"]])
    return {
        "provider": s.provider,
        "model": s.model or (prov.model if prov else ""),
        "small_model": s.small_model or (prov.small_model if prov else ""),
        "pinned": pinned,
        # a custom endpoint (e.g. OpenRouter) set via WAKU_BASE_URL / WAKU_API_KEY
        "base_url": s.base_url or "",
        "custom_key_set": bool(s.api_key),
        "providers": [
            {"name": name, "key_env": p.key_env,
             "key_set": bool(os.getenv(p.key_env)),
             "key_last4": (os.getenv(p.key_env) or "")[-4:],
             "default_model": p.model, "default_small_model": p.small_model}
            for name, p in PROVIDERS.items()
        ],
        # experimental tools (delegate_task -> pi). The ARENA can switch this on
        # per-race, but the chat agent reads it from the environment — so without
        # a toggle here, the sidebar chat could never delegate. See settings_save.
        "experimental": s.experimental,
        "pi_installed": bool(shutil.which("pi")),
        # graph workflows (triage-first turns) — same toggle contract as
        # experimental: the UI renders it, settings_save writes it.
        "graph_workflows": s.graph_workflows,
        # optional web-search key (Tavily) — same BYOK treatment as provider keys
        "search_key_env": "TAVILY_API_KEY",
        "search_key_set": bool(os.getenv("TAVILY_API_KEY")),
        "search_key_last4": (os.getenv("TAVILY_API_KEY") or "")[-4:],
        # episodic-memory backend: sqlite (default) or notion
        "episodic_store": s.episodic_store,
        "notion_token_set": bool(os.getenv("NOTION_TOKEN")),
        "notion_token_last4": (os.getenv("NOTION_TOKEN") or "")[-4:],
        "notion_db_set": bool(os.getenv("NOTION_EPISODES_DATABASE_ID")),
        "notion_db_last4": (os.getenv("NOTION_EPISODES_DATABASE_ID") or "")[-4:],
    }


def apply_settings(payload: dict) -> dict:
    """Write .env + os.environ, then rebuild the agent so the switch is live.
    Never logs keys; only whitelisted env names are writable."""
    from dotenv import find_dotenv, set_key

    from waku.loop.models import PROVIDERS

    provider = payload.get("provider")
    if provider not in PROVIDERS:
        return {"error": f"unknown provider {provider}"}
    episodic_store = payload.get("episodic_store")
    if episodic_store is not None and episodic_store not in ("sqlite", "notion"):
        return {"error": f"unknown episodic_store {episodic_store}"}
    before = {"provider": os.getenv("WAKU_PROVIDER", ""),
              "model": os.getenv("WAKU_MODEL", ""),
              "small_model": os.getenv("WAKU_SMALL_MODEL", "")}
    writable = ({"WAKU_PROVIDER", "WAKU_MODEL", "WAKU_SMALL_MODEL", "TAVILY_API_KEY",
                 "WAKU_EPISODIC_STORE", "WAKU_EXPERIMENTAL", "WAKU_GRAPH_WORKFLOWS",
                 "NOTION_TOKEN", "NOTION_EPISODES_DATABASE_ID"}
                | {p.key_env for p in PROVIDERS.values()})
    env_path = find_dotenv(usecwd=True) or ".env"

    updates = {"WAKU_PROVIDER": provider,
               "WAKU_MODEL": payload.get("model", "") or "",
               "WAKU_SMALL_MODEL": payload.get("small_model", "") or ""}
    if episodic_store:
        updates["WAKU_EPISODIC_STORE"] = episodic_store
    # NOT `if experimental:` — turning it OFF sends "", which is falsy. Absent
    # (None) means "don't touch"; "" means "switch it off".
    experimental = payload.get("experimental")
    if experimental is not None:
        updates["WAKU_EXPERIMENTAL"] = "1" if str(experimental).strip() else ""
    graph_workflows = payload.get("graph_workflows")  # same is-not-None rule
    if graph_workflows is not None:
        updates["WAKU_GRAPH_WORKFLOWS"] = "1" if str(graph_workflows).strip() else ""
    # Changing provider never carries a model across endpoints (live bug:
    # kimi->gemini kept gate model kimi-k3 and every turn 404'd on Gemini). But
    # if the user didn't newly type a model, use THIS provider's default (their
    # first pinned model for it, else its built-in default) — "a default model
    # per API key". An explicit model in the payload (e.g. from the chat pill)
    # always wins.
    if provider != before["provider"]:
        if updates["WAKU_MODEL"] in ("", before["model"]):
            updates["WAKU_MODEL"] = catalog.default_model_for(provider)
        if updates["WAKU_SMALL_MODEL"] in ("", before["small_model"]):
            updates["WAKU_SMALL_MODEL"] = ""
    for k, v in (payload.get("keys") or {}).items():
        if k in writable and v:  # only non-empty keys overwrite
            if k == "NOTION_EPISODES_DATABASE_ID":
                from waku.memory.episodic.notion_store import normalize_database_id

                try:
                    v = normalize_database_id(v)
                except ValueError as exc:
                    return {"error": str(exc)}
            updates[k] = v
    for k, v in updates.items():
        if k in writable:
            set_key(env_path, k, v)
            os.environ[k] = v

    error = browser_agent.rebuild()
    if error:
        return {"error": error}
    # a model/provider switch is a RELEASE event (the whiteboard's "new model
    # config" box) — record it in the trace so brain swaps are auditable
    browser_agent.current().tracer.event("config", {
        "from": before,
        "to": {"provider": provider, "model": updates["WAKU_MODEL"],
               "small_model": updates["WAKU_SMALL_MODEL"]},
    })
    return {"ok": True, **settings_info()}
