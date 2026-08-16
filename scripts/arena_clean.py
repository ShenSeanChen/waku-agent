"""Wipe the hosted memory partitions the Arena and the quickstarts write to.

    python scripts/arena_clean.py              # dry run: lists, deletes nothing
    python scripts/arena_clean.py --yes        # actually delete

WHY THIS EXISTS

sqlite and LangMem need no cleanup. Every Arena contestant runs in a throwaway
temp home, so a race never opens `.waku/`, and LangMem's default store is a
dict that dies with the process. Both start clean whether you ask them to or
not.

The hosted two do not. mem0 and Zep are keyed by a user id on YOUR account, so
every race and every quickstart run piles up in the same place, and by the
third session you cannot tell your results from last week's -- or from the
demo data the vendor seeded when you signed up.

That ambiguity is not cosmetic. Zep builds ONE graph per project and dedupes
entities into it, so a brand-new user id inherits concepts nobody ever told it:
a fresh `quickstart-zep` came back knowing about `brand deal`, `rough cut`,
`CTR` and `retention rate`, all of which trace to Zep's own onboarding sandbox.
mem0 does not do this -- rows carry a user_id and the wall holds -- which is
the difference between a row store and a graph in one sentence.

The consequence for benchmarking: giving each race a fresh user id DOES NOT
isolate it. You have to empty the project first. Hence this script, and hence
`--yes`, following the same rule as scripts/demo_seed.py -- nothing
irreversible happens because a script ran, only because a human typed --yes.

WHAT IT WILL NOT TOUCH

Your `.waku/` home. Your real assistant's memories live under the `waku` user
on the hosted services too, so that partition is listed but NEVER deleted
without --include-waku. Read the dry run before you trust the flag.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

load_dotenv()

# Partitions this repo is known to write to. `waku` is deliberately absent --
# it is the real assistant's, and it takes an extra flag.
ARENA_PREFIXES = ("quickstart-", "waku-arena-", "waku-latency-", "iso-test-")
REAL = "waku"


def mem0_users() -> tuple[object, list[str]]:
    from mem0 import MemoryClient

    client = MemoryClient()
    # get_all(filters={}) is a 400 -- mem0 requires a non-empty filter. users()
    # is the enumeration endpoint, and it flags vendor playground entities.
    payload = client.users()
    rows = payload.get("results", payload) if isinstance(payload, dict) else payload
    users = sorted(
        (r.get("name"), bool(r.get("is_playground")))
        for r in rows or [] if isinstance(r, dict) and r.get("name")
    )
    return client, users


def zep_users() -> tuple[object, list[str]]:
    from zep_cloud import Zep

    client = Zep(api_key=os.environ["ZEP_API_KEY"])
    found = client.user.list_ordered()
    users = [(u.user_id, False) for u in (getattr(found, "users", found) or [])
             if getattr(u, "user_id", None)]
    return client, sorted(users)


def hosted_stores() -> set[str]:
    """Which hosted backends is the REAL assistant configured to write to?

    Without this the `waku` partition looks precious and gets spared forever.
    On an install that runs sqlite -- the default -- a `waku` partition on
    mem0 or Zep cannot contain anything but leftovers from past races, and
    protecting it just means the contamination never actually goes away.
    """
    return {os.getenv("WAKU_SEMANTIC_STORE", "sqlite").strip().strip("'\"").lower(),
            os.getenv("WAKU_EPISODIC_STORE", "sqlite").strip().strip("'\"").lower()}


def classify(user: str, playground: bool, include_waku: bool,
             service_is_live: bool = True) -> tuple[bool, str]:
    """(delete?, why) -- said out loud so a dry run is readable."""
    if user == REAL:
        if not service_is_live:
            return (True, ("arena leftovers -- your assistant is configured for "
                           "sqlite, so nothing here is its memory"))
        return (include_waku, "your real assistant's partition"
                + ("" if include_waku else " -- kept, pass --include-waku to remove"))
    if user.startswith(ARENA_PREFIXES):
        return (True, "written by this repo (arena or quickstart)")
    if playground:
        return (True, "vendor playground data -- never written by this repo")
    # Zep's onboarding sandbox is the documented source of the cross-user
    # entity bleed, so leaving it in place defeats the whole cleanup.
    return (True, "not written by this repo -- vendor demo/sandbox data")


def main(apply: bool, include_waku: bool) -> None:
    plan: list[tuple[str, str, str, str]] = []  # (service, user, verdict, why)
    live = hosted_stores()
    print(f"assistant is configured for: {', '.join(sorted(live))}\n")

    for name, fetch in (("mem0", mem0_users), ("zep", zep_users)):
        try:
            client, users = fetch()
        except Exception as ex:
            print(f"{name}: skipped -- {type(ex).__name__}: {ex}")
            continue
        for user, playground in users:
            do, why = classify(user, playground, include_waku, service_is_live=name in live)
            plan.append((name, user, "DELETE" if do else "keep", why))
        globals()[f"_{name}_client"] = client

    if not plan:
        print("nothing found on either service.")
        return

    print(f"{'service':8} {'verdict':7} {'user':28} why")
    print("-" * 92)
    for service, user, verdict, why in plan:
        print(f"{service:8} {verdict:7} {user:28} {why}")

    doomed = [(s, u) for s, u, v, _ in plan if v == "DELETE"]
    print(f"\n{len(doomed)} partition(s) would be deleted.")

    if not apply:
        print("\nDry run -- nothing was deleted. This is irreversible; if you mean it:")
        print("    python scripts/arena_clean.py --yes")
        return

    for service, user in doomed:
        client = globals()[f"_{service}_client"]
        try:
            if service == "mem0":
                client.delete_all(user_id=user)
            else:
                client.user.delete(user_id=user)
            print(f"deleted {service}:{user}")
        except Exception as ex:
            print(f"FAILED {service}:{user} -- {type(ex).__name__}: {ex}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Wipe hosted memory partitions (mem0, Zep).")
    p.add_argument("--yes", "-y", action="store_true",
                   help="actually delete. Without it this only lists what it would do.")
    p.add_argument("--include-waku", action="store_true",
                   help="also delete the `waku` partition -- your real assistant's memories.")
    args = p.parse_args()
    main(apply=args.yes, include_waku=args.include_waku)
