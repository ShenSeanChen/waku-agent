"""The small Settings surface left after Connections owns integrations.

Provider credentials, models, memory backends, search and gateways are managed
by :mod:`waku.integrations`. This module retains the Experimental and
Graph-workflows toggles and the model pin action; catalog remains the sole
owner of pin persistence.
"""

from __future__ import annotations

import shutil

from waku.config import load_settings
from waku.loop.models import PROVIDERS
from waku.ops import catalog


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
    s = load_settings()
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
    # Resolve the model the same way the loop does. `waku/loop/models.py` fills a
    # blank WAKU_MODEL from the provider's default at build time, so the agent is
    # always running SOMETHING — but this dict is what the nav pill and the Models
    # page render, and reporting "" made a fresh install display `anthropic ·`,
    # a trailing separator with no model name. The display must not claim less
    # than the agent actually has.
    prov = PROVIDERS.get(s.provider)
    return {
        "provider": s.provider,
        "model": s.model or (prov.model if prov else ""),
        "small_model": s.small_model or (prov.small_model if prov else ""),
        "base_url": s.base_url or "",
        "custom_key_set": bool(s.api_key),
        # Ids of providers the user disabled in the Models grid; the frontend
        # derives each card's status (unconfigured / configured / enabled) and
        # hides disabled providers from the chat switcher.
        "disabled_providers": sorted(s.disabled_providers),
        "pinned": pinned,
        "providers": [{"name": name} for name in PROVIDERS],
        # experimental tools (delegate_task -> pi). The ARENA can switch this on
        # per-race, but the chat agent reads it from the environment — so without
        # a toggle here, the sidebar chat could never delegate. See settings_save.
        "experimental": s.experimental,
        "pi_installed": bool(shutil.which("pi")),
        # graph workflows (triage-first turns) — same toggle contract as
        # experimental: the UI renders it, apply_settings writes it.
        "graph_workflows": s.graph_workflows,
    }


def apply_settings(payload: dict) -> dict:
    """Save the remaining Settings concerns: the experimental and graph-workflows
    toggles.

    Connection fields and provider changes deliberately live in integrations.
    """
    import os

    from dotenv import find_dotenv, set_key

    if "episodic_store" in payload:
        return {"error": "episodic_store is managed in Connections"}
    env_path = find_dotenv(usecwd=True) or ".env"
    # NOT `if toggle:` — turning it OFF sends "", which is falsy. Absent (None)
    # means "don't touch"; "" means "switch it off".
    toggles = (("experimental", "WAKU_EXPERIMENTAL"), ("graph_workflows", "WAKU_GRAPH_WORKFLOWS"))
    for field, env_name in toggles:
        value = payload.get(field)
        if value is not None:
            value = "1" if str(value).strip() else ""
            set_key(env_path, env_name, value)
            os.environ[env_name] = value
    return {"ok": True, **settings_info()}
