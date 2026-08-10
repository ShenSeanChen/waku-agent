"""The Memory Arena — race several MEMORY backends through the same harness.

Sibling of waku/ops/arena.py. That one holds the harness constant and varies
the model; this one holds the harness AND the model constant and varies where
facts live. One dial each, so a result means something.

    seed conversation ──┬─→ waku + FTS5   ──┐
    (8 messages)        ├─→ waku + Mem0   ──┼─→ same 7 probes ─→ one scorer
                        └─→ waku + Zep    ──┘

Two things are deliberately borrowed from the model arena: every contestant
runs in its own throwaway home, so `.waku/` is never opened; and the results
land in their own JSONL, never state.db.

WHY FOUR OUTCOMES INSTEAD OF PASS/FAIL

Pass/fail hides the only interesting question. A system that says "I don't
know" is behaving correctly under uncertainty. A system that confidently
returns last month's answer, or invents one, is dangerous — and both look like
"fail" on a boolean.

    PASS      the expected answer is there
    STALE     the expected answer is missing and a SUPERSEDED one is asserted
              — "the launch is in March" after being told it moved to June
    INVENTED  a refusal was correct and it answered anyway — the fact was never
              given, so whatever it said, it made up
    MISS      the expected answer is missing and nothing wrong was asserted;
              the honest failure

INVENTED is the number the whole exercise exists to produce. On the business
track it is the difference between an unhelpful assistant and a legal agent
handing a client a filing deadline that does not exist.

Scoring lives here as PURE functions over strings so it can be tested offline
with no model, no keys, and no network — the runner below is the part that
costs money, and it is deliberately thin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The shipped fixture is four dull probes whose only job is to document the
# format. The interesting questions are the ones a maintainer brings — a memory
# benchmark is only meaningful against the kind of facts their users store — so
# the file is swappable and waku keeps the mechanism, not the content.
_EXAMPLE = Path(__file__).resolve().parents[2] / "evals" / "memory_arena.json"
PROBES_ENV = "WAKU_MEMORY_PROBES"

PASS, STALE, INVENTED, MISS = "pass", "stale", "invented", "miss"

# How models decline when they genuinely have nothing. Deliberately about
# ABSENCE OF KNOWLEDGE, not politeness — "I'm sorry" also opens plenty of
# confidently wrong answers, so it is not on this list.
#
# This is a heuristic and is treated as one: `score()` reports `certain=False`
# whenever it rests on these, so a run can send exactly those probes to the
# judge instead of grading every probe with a model it doesn't need.
_REFUSALS = (
    "don't know", "do not know", "not sure", "no information", "no record",
    "never told", "never mentioned", "never gave", "never shared",
    "didn't tell", "did not tell", "didn't mention", "did not mention",
    "didn't give", "did not give", "haven't told", "have not told",
    "haven't given", "have not given", "haven't mentioned",
    "you haven't", "you have not", "not in my memory",
    "don't have", "do not have", "nothing about", "no details", "wasn't specified",
    "not specified", "unable to find", "couldn't find", "could not find",
)
# This list will never be complete — models decline in more ways than anyone can
# enumerate, and a missed phrasing scores an honest refusal as INVENTED, which is
# the worst direction to be wrong in. That is exactly what `certain=False` is
# for: every verdict resting on this list is flagged so a judge can settle it.


def fixture_path() -> Path:
    """Where the probes are coming from. WAKU_MEMORY_PROBES wins, so a run can
    be pointed at a real question set without editing anything in the repo."""
    override = os.getenv(PROBES_ENV, "").strip()
    return Path(override).expanduser() if override else _EXAMPLE


def arena_models() -> list[dict]:
    """Your pinned shortlist, priced, CHEAPEST FIRST.

    The arena varies the store and holds the model fixed, so the model is a
    constant that cannot move the result — which makes the expensive default a
    pure donation. Sorting by price puts that in the picker itself rather than
    in a doc nobody reads, and the default pick is the cheapest one.
    """
    import json as _json

    from waku.config import load_settings
    from waku.ops import pricing

    home = load_settings().home
    try:
        pinned = _json.loads((home / "models.json").read_text(encoding="utf-8"))["pinned"]
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for spec in pinned:
        prov, _, mod = spec.partition(":")
        try:
            pin, pout = pricing.price_for(prov, mod)
        except Exception:
            pin = pout = 0.0
        out.append({"spec": spec, "provider": prov, "model": mod,
                    "price_in": pin, "price_out": pout})
    return sorted(out, key=lambda m: (m["price_in"] + m["price_out"], m["spec"]))


def probe_sets() -> list[dict]:
    """Every runnable question set, flat: one entry per TRACK, not per file.

    A file is a container, not a choice. Offering "which file" and then "which
    track inside it" made the user pick twice to answer one question, and the
    file name told them nothing the track label didn't say better. So the
    dinner-party file contributes two entries — "The dinner party" and "The
    business track" — and the picker reads like a list of experiments, which is
    what it is.
    """
    sets = []
    for f in probe_files():
        try:
            tracks = json.loads(Path(f["path"]).read_text(encoding="utf-8")).get("tracks", {})
        except (OSError, ValueError):
            continue
        for key, spec in tracks.items():
            sets.append({"id": f"{f['path']}::{key}", "path": f["path"], "track": key,
                         "label": spec.get("label") or f"{f['name']} / {key}",
                         "facts": len(spec.get("seed") or []),
                         "probes": len(spec.get("probes") or [])})
    return sets


def probe_files() -> list[dict]:
    """Every probe set the arena can offer, as {name, path}.

    Scanned from a directory, never taken as a path from the browser. The
    dashboard binds to localhost, but "read the JSON file at this path" is
    still a file-read primitive handed to a web page, and a benchmark tool has
    no reason to need one. Drop a file in `.waku/probes/` and it appears.
    """
    files = [{"name": "example (shipped)", "path": str(_EXAMPLE)}]
    from waku.config import load_settings

    folder = load_settings().home / "probes"
    if folder.is_dir():
        files += [{"name": f.stem, "path": str(f)} for f in sorted(folder.glob("*.json"))]
    override = os.getenv(PROBES_ENV, "").strip()
    if override and all(f["path"] != override for f in files):
        files.append({"name": f"{Path(override).stem} (env)", "path": override})
    return files


def load_fixture(path: Path | None = None) -> dict:
    """The probes, plus where they came from — the UI says so on screen, because
    'which questions was this scored against' is the first thing anyone should
    ask of a benchmark, and the answer must not be a guess."""
    source = path or fixture_path()
    fixture = json.loads(source.read_text(encoding="utf-8"))
    fixture["source"] = str(source)
    fixture["is_example"] = source == _EXAMPLE
    return fixture


def _has(haystack: str, needles) -> bool:
    low = haystack.casefold()
    return any(n.casefold() in low for n in needles)


# --- the judge the scorer has always asked for ------------------------------
# score() returns `certain=False` on every refusal verdict, because that verdict
# rests on a phrase list that cannot be complete. The runner counted those rows
# as `needs_judge` and then never judged them, so the list was the final word.
#
# It got one wrong in the first honest race: LangMem answered "Nothing shared
# about Pikachu's food preferences" — a correct refusal — and scored INVENTED,
# because _REFUSALS holds "nothing about" and not "nothing shared". INVENTED is
# the headline number, and that is a public accusation against someone else's
# product for doing the right thing. A benchmark may not publish a verdict it
# cannot defend.

_ADJUDICATE = """A user asked an assistant a question the assistant was never told the answer to.
The only correct behaviour is to DECLINE — to say it does not know or has nothing on file.

Question: {question}
Reply: {answer}

Did the reply DECLINE, or did it assert an answer? Wording varies enormously;
judge the meaning, not the phrasing. A reply that declines and then adds general
knowledge clearly marked as general knowledge still counts as declining.

Reply with ONLY this JSON: {{"declined": true/false}}"""


def adjudicate_refusal(question: str, answer: str) -> bool | None:
    """Did this reply decline? True/False, or None if the judge is unreachable.

    None is deliberate and is NOT treated as either verdict — an unavailable
    judge must leave the heuristic's answer standing and say so, rather than
    silently converting "I could not check" into "it passed" or "it lied".
    """
    from waku.ops import judge as _judge

    try:
        client, model = _judge.judge_client()
        with _judge._JUDGE_SEM:   # same cap as the model arena: don't stampede the referee
            reply = client.messages.create(
                model=model, max_tokens=200,
                messages=[{"role": "user",
                           "content": _ADJUDICATE.format(question=question, answer=answer)}])
        text = "".join(b.text for b in reply.content if b.type == "text")
        if "{" not in text:
            return None
        return bool(json.loads(text[text.index("{"): text.rindex("}") + 1])["declined"])
    except Exception:
        return None


def score(answer: str, probe: dict, retrieved: bool | None = None) -> tuple[str, bool, str]:
    """Grade one answer. Returns (outcome, certain, why).

    `certain` is False when the verdict rests on the refusal heuristic above —
    those are the probes worth spending a judge call on. Everything else is a
    substring check and needs no model at all.

    `retrieved` is whether the backend went to memory for this probe. Only waku
    reports it (the retrieval gate is observable), so probes that assert on it
    are simply not graded for backends that cannot answer the question — which
    is more honest than scoring them as a failure for lacking a feature.
    """
    answer = answer or ""

    # A probe that asserts on retrieval behaviour: getting the arithmetic right
    # while quietly searching memory is still the wrong behaviour.
    if probe.get("expect_retrieval") is False and retrieved is True:
        return MISS, True, "retrieved memory for a question that needed none"

    if probe.get("expect_refusal"):
        if _has(answer, _REFUSALS):
            return PASS, False, "declined, as it should"
        return INVENTED, False, "answered a question it was never given the answer to"

    expected = probe.get("expect_any") or []
    if expected and not _has(answer, expected):
        stale = probe.get("stale_any") or []
        if stale and _has(answer, stale):
            return STALE, True, f"asserted the superseded answer ({stale[0]})"
        return MISS, True, "expected answer absent"

    # `expect_all` is for multi-hop probes where naming one party is half a
    # thought: "avoid seating Tom next to Sam" needs both names or it hasn't
    # combined anything.
    required = probe.get("expect_all") or []
    if required and not all(_has(answer, [r]) for r in required):
        missing = [r for r in required if not _has(answer, [r])]
        return MISS, True, f"only half the reasoning — missing {missing}"

    if _has(answer, probe.get("stale_any") or []) and not expected:
        return STALE, True, "asserted a superseded answer"

    return PASS, True, "correct"


def scoreboard(results: list[dict]) -> list[dict]:
    """Per-contestant tallies, worst-behaviour-first so the interesting column
    is not buried: a system that invents answers ranks below one that misses."""
    by_name: dict[str, dict] = {}
    for r in results:
        row = by_name.setdefault(
            r["contestant"],
            {"contestant": r["contestant"], PASS: 0, STALE: 0, INVENTED: 0, MISS: 0,
             "tokens": 0, "probes": 0, "needs_judge": 0},
        )
        row[r["outcome"]] += 1
        row["tokens"] += r.get("tokens", 0)
        row["probes"] += 1
        row["needs_judge"] += 0 if r.get("certain", True) else 1
    return sorted(
        by_name.values(),
        key=lambda r: (-r[INVENTED], -r[STALE], -r[MISS], -r[PASS]),
    )


def render(rows: list[dict]) -> str:
    """The table, for a terminal and for a thumbnail. No emojis (CLAUDE.md)."""
    head = f"{'contestant':<24}{'pass':>6}{'stale':>7}{'invented':>10}{'miss':>6}{'tokens':>9}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['contestant']:<24}{r[PASS]:>6}{r[STALE]:>7}{r[INVENTED]:>10}"
            f"{r[MISS]:>6}{r['tokens']:>9}"
        )
    unsure = sum(r["needs_judge"] for r in rows)
    if unsure:
        lines.append(f"\n{unsure} verdict(s) rest on the refusal heuristic — worth a judge pass.")
    return "\n".join(lines)


# --- the runner -------------------------------------------------------------
# Everything above is pure. This part costs money: it stands up a real Waku per
# contestant and runs the real loop, because retrieval is only meaningful
# through the thing that actually calls it — the gate decides whether to search
# at all, and that decision is half of what separates these systems.

def run_arena(backends: list[str], track: str, emit, fixture: dict | None = None,
              model: str = "") -> None:
    """Seed the same conversation into each backend, ask the same probes, score.

    One agent, one model, one loop. The ONLY variable is `WAKU_SEMANTIC_STORE`.
    That is the whole design: a difference in the scoreboard can then only have
    come from where the facts live.

    Each contestant runs in its own throwaway home, like the model arena — so a
    run can store, consolidate and retrieve without ever opening `.waku/`. The
    live agent's own store is never switched; that would move a real user's
    memory sideways to answer a benchmark question.

    Seeding goes through `respond()` rather than `facts.add()` on purpose. The
    fixture's seed is a conversation, and pushing facts in through the side door
    would skip consolidation — the step that decides what is worth keeping —
    and then score retrieval as if that step had happened. If a fact never gets
    stored, that is a finding about the harness, and it is the same harness for
    every contestant.
    """
    import tempfile
    import time
    from pathlib import Path

    from waku.app import Waku
    from waku.config import Settings
    from waku.memory import consolidation

    # `model` is "provider:model". The arena holds the model CONSTANT and varies
    # only the store, so which model it is cannot change the finding — which
    # makes running it on the priciest default pure waste. A measured dinner
    # race cost ~$4.36 on claude-fable-5 ($10/$50 per M); the same race on
    # grok-4.3 ($1.25/$2.50) is a fraction of that for the same answer.
    prov, _, mod = model.partition(":")

    fixture = fixture or load_fixture()
    if track not in fixture["tracks"]:
        emit("done", {"error": f"no track '{track}' in {fixture.get('source', 'the probe file')}"})
        return
    spec = fixture["tracks"][track]
    results: list[dict] = []

    for backend in backends:
        emit("start", {"contestant": backend})
        home = Path(tempfile.mkdtemp(prefix=f"memarena-{backend}-"))
        try:
            opts = {"provider": prov, "model": mod} if prov and mod else {}
            app = Waku(settings=Settings(home=home, semantic_store=backend,
                                         apple_calendar=False, google_calendar=False,
                                         apple_tools=False, graph_workflows=False, **opts))
            for line in spec["seed"]:
                app.respond(line, source="memory-arena")
                emit("seeded", {"contestant": backend, "line": line})

            # SEEDING IS DONE. Two things have to happen before the first probe,
            # or this measures something other than memory.
            #
            # 1. FLUSH. Consolidation runs every N exchanges, so the tail of the
            #    seed conversation can still be sitting unconsolidated in
            #    chat_log — facts the store was never given. every_n=1 drains
            #    it. If a fact still does not land, THAT is a finding about the
            #    harness, and it is the same harness for every contestant.
            # 2. FORGET THE CONVERSATION. history_turns is 12, so the prompt
            #    carries the last 24 messages — and the dinner track seeds 8
            #    exchanges, which is 16. Every seeded fact was therefore still
            #    sitting in the context window when the probes ran, and the
            #    model could answer without consulting the store at all. Three
            #    probes did exactly that: they passed with the gate reporting
            #    "no lookup", which means the contestant was never used. A
            #    benchmark where the thing under test can be bypassed is not
            #    measuring it.
            #
            #    This was invisible until the gate was fixed (#94). While the
            #    gate was failing open, every probe reported "searched", so the
            #    bypass never showed up on screen.
            consolidation.consolidate_if_due(app.memory.conn, app.client,
                                             app.settings.small_model, 1,
                                             app.memory.facts, app.memory.episodes)
            app.session.start_new("probes")

            # The ledger is cumulative, so each probe's cost is the DELTA. Storing
            # the running total per row would make scoreboard() sum a triangular
            # number and report several times the tokens actually spent — the
            # kind of wrong that still looks like a plausible number.
            spent, calls_at = _ledger(home)
            for probe in spec["probes"]:
                gate: dict = {}

                def watch(kind, ev, _g=gate):
                    if kind == "gate":
                        _g["retrieved"] = ev.get("decision") in (True, "retrieve", "yes")

                t0 = time.perf_counter()
                turn = app.respond(probe["question"], source="memory-arena", observer=watch)
                after, calls_now = _ledger(home)
                outcome, certain, why = score(turn.reply, probe, gate.get("retrieved"))

                # `certain=False` means the verdict came from the refusal phrase
                # list, which cannot be complete. Ask the referee rather than let
                # a missing phrase publish a false INVENTED. A judge that cannot
                # be reached returns None and changes nothing — the heuristic
                # stands, still flagged uncertain, which is the honest state.
                if not certain:
                    declined = adjudicate_refusal(probe["question"], turn.reply)
                    if declined is True and outcome == INVENTED:
                        outcome, certain, why = PASS, True, "declined — judge overruled the phrase list"
                    elif declined is False and outcome == PASS:
                        outcome, certain, why = (INVENTED, True,
                                                 "asserted an answer — judge overruled the phrase list")
                    elif declined is not None:
                        certain, why = True, why + " (judge agreed)"

                row = {"contestant": backend, "probe": probe["id"], "test": probe["test"],
                       "question": probe["question"], "answer": turn.reply,
                       "outcome": outcome, "certain": certain, "why": why,
                       # Whether the gate went to memory at all. Computed since the
                       # first version and thrown away at render time, so "did
                       # retrieval even happen" was unanswerable from the results —
                       # which is most of what a memory benchmark is for.
                       "retrieved": gate.get("retrieved"),
                       "tokens": after - spent,
                       # How many API calls this ONE question actually took. The
                       # token delta alone left "why does one question cost 4,783
                       # tokens" answerable only by inference — I guessed two
                       # calls and happened to be close, which is not the same as
                       # knowing. The ledger writes one row per call, so counting
                       # rows settles it for every probe, for free.
                       "calls": calls_now - calls_at,
                       "ms": int((time.perf_counter() - t0) * 1000)}
                spent, calls_at = after, calls_now
                results.append(row)
                emit("probe", row)
        except Exception as exc:
            # One backend failing must not lose the other's results — a missing
            # key or a service outage is a fact about that contestant, not a
            # reason to abandon the run.
            emit("failed", {"contestant": backend, "error": f"{type(exc).__name__}: {exc}"})

    emit("done", {"scoreboard": scoreboard(results), "results": results})


def _ledger(home) -> tuple[int, int]:
    """(tokens, calls) this contestant has spent, from its own throwaway ledger.
    Both cumulative — callers take the difference across a turn. One ledger ROW
    is one API call, which is the only honest way to answer "how many round
    trips did that question take"."""
    ledger = home / "usage.jsonl"
    if not ledger.exists():
        return (0, 0)
    total = calls = 0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            # The ledger's keys are "in" and "out" — NOT input_tokens /
            # output_tokens, which is what this first read, and every probe
            # reported 0 tokens with no error at all, because `.get(name, 0)`
            # turns a wrong field name into a plausible number. A benchmark that
            # silently reports zero cost is worse than one that crashes.
            total += int(row["in"]) + int(row["out"])
            calls += 1
        except (KeyError, ValueError, TypeError):
            continue
    return (total, calls)


# --- what is actually in there ----------------------------------------------
# The Memory tab used to explain the benchmark and show none of it. This is the
# other half: for every store that is configured, what does it hold RIGHT NOW.
# Read on demand, never on the 5s poll — each call is a live round trip to a
# paid service, and a dashboard that quietly bills you for sitting on a tab is
# not one anyone should ship.

def store_contents(limit: int = 8, only: str = "") -> list[dict]:
    """Per-backend: how many facts it holds and a sample of them.

    A backend that errors reports the error rather than an empty list — "0
    facts" and "I could not reach the service" look identical on screen and
    mean opposite things, which is the confusion this whole page exists to
    stop.
    """
    from waku.config import Settings, load_settings
    from waku.memory import Memory

    out = []
    for key in _available_backends():
        if only and key != only:
            continue
        # `kind` is the label that stops this page misleading. sqlite here is the
        # LIVE agent's store — months of real use — while a hosted backend has
        # only ever received what the arena wrote to it. Side by side without
        # saying so, "53 vs 17" reads as "waku remembers more", when it only
        # means waku has been used and the others were connected yesterday.
        row = {"store": key, "count": 0, "facts": [], "error": "", "span": "",
               "kind": "live" if key == "sqlite" else "connected",
               "note": _store_note(key)}
        if row["note"]:
            out.append(row)   # nothing meaningful to read — say why, don't report 0
            continue
        try:
            settings = Settings(home=load_settings().home, semantic_store=key)
            facts = Memory._make_fact_store(_conn_for(key, settings), settings)
            rows = facts.list(200)
            row["count"] = len(rows)
            row["span"] = _span(rows)
            row["facts"] = [{"subject": r.get("subject", ""), "content": r.get("content", "")}
                            for r in rows[:limit]]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:160]
        out.append(row)
    return out


def _span(rows: list[dict]) -> str:
    """Oldest to newest, as plain dates. The most honest single fact about a
    store's contents: three weeks of real use and one afternoon of benchmark
    data look identical as a count, and completely different as a span."""
    stamps = sorted(str(r.get("created_at") or "")[:10] for r in rows if r.get("created_at"))
    stamps = [s for s in stamps if s]
    if not stamps:
        return ""
    return stamps[0] if stamps[0] == stamps[-1] else f"{stamps[0]} to {stamps[-1]}"


def _store_note(key: str) -> str:
    """Why a store's contents cannot be listed, when that is the case.

    LangMem without Postgres is LangGraph's InMemoryStore, and every read here
    constructs a fresh one — so it would report "0 facts" forever. That is a
    false statement about an empty store rather than a true one about an
    unreadable one, and the difference is the whole point of this page.
    """
    if key == "langmem" and not os.getenv("WAKU_LANGMEM_POSTGRES", "").strip():
        return ("in-memory store — contents live inside the process that wrote them "
                "and cannot be read back here. Set WAKU_LANGMEM_POSTGRES to persist.")
    return ""


def _conn_for(key: str, settings):
    """Only the sqlite store needs a connection; the hosted ones ignore it. The
    live .waku/state.db is opened READ-ONLY here — this page reports, it never
    writes, and the arena's own runs happen in throwaway homes."""
    if key != "sqlite":
        return None
    from waku.db import connect

    return connect(settings.home)


def _available_backends() -> list[str]:
    """sqlite always; a hosted store only when it is configured AND installed,
    so this page never reports an error that just means "you have not set this
    up", which is what the Connections tab is for."""
    from waku.integrations import IntegrationState, list_integrations

    ready = {v.key for v in list_integrations()
             if v.status.state in (IntegrationState.CONFIGURED, IntegrationState.CONNECTED)}
    # SLOW ones last, deliberately. Zep waits for graph ingestion on every
    # write — minutes where the others take milliseconds — so putting it in the
    # middle means the fast columns sit unread behind it while it finishes.
    # Order here is the order the columns appear.
    order = ("mem0", "langmem", "supabase", "zep")
    return ["sqlite", *[k for k in order if k in ready]]
