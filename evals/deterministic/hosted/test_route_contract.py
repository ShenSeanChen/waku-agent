"""Every route the dashboard serves has a hosted policy entry -- acceptance 5.

A waku upgrade needs no gateway change unless the route list changed. A5's
test_dashboard_routes.py pins that list in both directions; this one pins the
policy table against it, also in both directions and in one assertion.

WHY ONE ASSERTION. In A5's review a guard reported a single bad key, the fix
pinned that key, the suite went green, and a live route was still unpinned.
Set equality cannot do that: both differences are computed fresh on every run
and the test passes only when both are empty. Fixing what it reports cannot
mask what it did not report.

The pinned sets are loaded by path rather than imported by name. pytest's
sys.path handling puts a test file's own directory on the path, and this file
is one directory below the one holding test_dashboard_routes.py -- so a plain
import would work only when the other file happened to be collected first.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hosted.core import policy

ROOT = Path(__file__).resolve().parents[3]
PINNED_SOURCE = ROOT / "evals" / "deterministic" / "test_dashboard_routes.py"


def _load_pinned_module():
    spec = importlib.util.spec_from_file_location("waku_pinned_routes", PINNED_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pinned_routes() -> set[str]:
    module = _load_pinned_module()
    return set(module.PINNED_GET) | set(module.PINNED_POST)


# The only two policy entries that are not routes of their own. Both are
# prefixes, and both exist so that a family stays denied by default:
#   "/"              the catch-all, which says out loud what design section 8
#                    already decides -- a path with no entry passes
#   "/api/compare"   so a compare route added upstream is blocked rather than
#                    passed; "/api/compare/history" is longer and passes
ALLOWED_EXTRA_ENTRIES = {"/", "/api/compare"}

# The whole table, verdict by verdict, from design section 8. Pinning the KEYS
# alone would let a flip from block to pass on any arena route through the
# entire suite -- and those are the routes design section 8 blocks because "a
# race calls several models at once", which is several times the spend.
EXPECTED = {
    # pass: per-tenant by construction -- own process, own home
    "/": "pass",
    "/static/": "pass",
    "/api/data": "pass",
    "/api/events": "pass",
    "/api/session": "pass",
    "/api/memory": "pass",
    "/api/chat": "pass",
    "/api/chat/stream": "pass",
    "/api/graph/stream": "pass",
    "/api/pin": "pass",
    "/api/models": "pass",
    "/api/query": "pass",
    "/api/compare/history": "pass",
    # filter: the payload decides
    "/api/providers": "filter",
    "/api/settings": "filter",
    "/api/connections": "filter",
    "/api/connections/test": "filter",
    # block in the MVP
    "/api/compare": "block",
    "/api/compare/clear": "block",
    "/api/compare/regrade": "block",
    "/api/compare/delete_run": "block",
    "/api/compare/stream": "block",
    "/api/memory-arena": "block",
    "/api/memory-arena/stores": "block",
    "/api/memory-arena/clean": "block",
    "/api/memory-arena/stream": "block",
    "/api/judgment-arena": "block",
    "/api/judgment-arena/key": "block",
    "/api/judgment-arena/stream": "block",
    "/api/voice": "block",
    "/api/reveal": "block",
}


def test_every_routes_verdict_is_pinned_not_just_its_presence():
    """Set equality on the keys says every route was classified. This says
    HOW. A reviewer changing one of these has to change this literal too,
    which is the review signal the arena blocks are worth."""
    assert policy.DECISIONS == EXPECTED, (
        "\n".join(f"  {route}: {policy.DECISIONS.get(route, '(absent)')} "
                  f"but pinned as {EXPECTED.get(route, '(unpinned)')}"
                  for route in sorted(set(policy.DECISIONS) | set(EXPECTED))
                  if policy.DECISIONS.get(route) != EXPECTED.get(route)))


def test_the_policy_table_is_exactly_the_pinned_routes_plus_two_prefixes():
    pinned = _pinned_routes()
    classified = set(policy.DECISIONS)
    assert classified == pinned | ALLOWED_EXTRA_ENTRIES, (
        "\nroutes the dashboard serves with no entry in hosted/core/policy.py:\n  "
        + "\n  ".join(sorted(pinned - classified) or ["(none)"])
        + "\npolicy entries for routes the dashboard no longer serves:\n  "
        + "\n  ".join(sorted(classified - pinned - ALLOWED_EXTRA_ENTRIES) or ["(none)"])
        + f"\n\nThe only entries allowed beside the pinned set are "
          f"{sorted(ALLOWED_EXTRA_ENTRIES)}. Classify a new route in "
          "hosted/core/policy.py: pass, block, or filter.")


def test_the_pinned_set_is_the_one_the_dashboard_actually_serves():
    """A5 asserts this too. Repeated here because this file's whole claim
    rests on the pinned set being live rather than a list someone once wrote."""
    module = _load_pinned_module()
    assert module.routes_in_dashboard() == set(module.PINNED_GET) | set(module.PINNED_POST)


def test_every_pinned_route_resolves_to_its_own_entry_not_a_shorter_one():
    """Longest-prefix matching is what makes /api/memory-arena/stores block
    while /api/memory passes. A route whose own entry is shadowed by a shorter
    one would be classified by the wrong row and nobody would see it."""
    wrong = {route: policy.match(route) for route in _pinned_routes()
             if policy.match(route) != route}
    assert not wrong, f"these routes match a different entry than their own: {wrong}"
