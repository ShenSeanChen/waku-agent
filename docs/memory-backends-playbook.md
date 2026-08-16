# Seeing your memories in each provider's own console

The Arena's **Memory** tab reads every backend through Waku's `FactStore`, so it
shows you the same six methods for all of them. That is the point of the
contract — and it is also its blind spot. Each provider stores something
*different* underneath, and the only way to see what they actually did with your
sentences is to open their own console.

This is the walkthrough for that: where the data lands, what you will see there
that Waku's view flattens away, and how to clean up after a benchmark run.

| backend | console | what Waku's view hides |
|---|---|---|
| Mem0 | [app.mem0.ai](https://app.mem0.ai) → Memories | its extraction decisions, per-memory history |
| Zep | [app.getzep.com](https://app.getzep.com) → your project → Graph | the entity/edge graph and validity intervals |
| LangMem | **none — it is a library, not a service** | nothing; there is no server to look at |
| Supabase | your project → Table editor → the vector table | raw rows and embeddings |
| sqlite | Waku's own **Memory** page, or `.waku/state.db` | nothing — this one is fully visible already |

---

## Before anything: which account and which user are you looking at?

Every hosted backend partitions by a user id, and Waku writes under a stable
default so a single-user install does not scatter its memories across a new
partition per run.

| backend | env var | default |
|---|---|---|
| Mem0 | `MEM0_USER_ID` | `waku` |
| Zep | `ZEP_USER_ID` | `waku` |
| LangMem | *(namespace is fixed in code)* | — |

If a console looks empty, check this first. Filtering by the wrong user id looks
exactly like "nothing was stored", and those two things mean opposite things.

---

## Mem0 — [app.mem0.ai](https://app.mem0.ai)

1. Sign in with the account that owns the `MEM0_API_KEY` in your `.env`.
2. Open **Memories** and filter by user `waku`.

**What to look for.** Mem0's product is deciding what is worth remembering, so
compare the sentence you said against the row it kept — they are usually not the
same text. That is the feature, not a bug.

**One caveat that changes how you read a race.** Waku's adapter calls `add()`
with `infer=False`, because the `FactStore` contract says a write must always
store. That deliberately turns Mem0's extraction *off*, so an Arena result
measures its **retrieval**, not its extraction. If you want to see extraction
working, add memories through Mem0's own playground rather than through Waku.

---

## Zep — [app.getzep.com](https://app.getzep.com)

1. Sign in, pick the project whose key is in `ZEP_API_KEY`.
2. **Users** → `waku` → **Graph** for the visual, or **Episodes** for the raw
   text you sent.

**What to look for — this is the one worth filming.** Zep does not keep rows, it
builds a temporal knowledge graph: entities, edges between them, and *validity
intervals*. Ask it for the launch time and it does not rank two competing facts;
the older edge is marked superseded at a point in time. That is a genuinely
different design from every row-based store in the list, and the graph view is
where you can see it.

**Two things that will confuse you if nobody says them:**

- **Ingestion is asynchronous.** `graph.add()` returns in about 0.2s with
  `processed=False`. Data you just wrote may not be searchable for a minute or
  more. Waku's adapter polls until Zep reports `processed=True` (capped by
  `ZEP_MAX_WAIT_SECONDS`, default 120) precisely so a race does not score Zep as
  having forgotten something it was still filing.
- **Episodes ≠ what you get back.** Search returns Zep's rendering of an edge or
  node, not the sentence you sent. Answers read differently from other backends
  even when equally correct.

---

## LangMem — there is no console

LangMem is a **library**, not a hosted service, and this trips people up because
it sits in the same list as two products with dashboards.

- **Without** `WAKU_LANGMEM_POSTGRES`, it runs LangGraph's `InMemoryStore` —
  memory inside the Python process. When the dashboard restarts, it is gone.
  There is nothing to open, and nothing to clean up.
- **With** `WAKU_LANGMEM_POSTGRES=postgresql://…`, it uses `PostgresStore`, and
  "viewing your memories" means querying that database directly.

This is also why the Arena's store card shows LangMem as unreadable rather than
as "0 facts" — every read would construct a fresh empty store, and reporting
that as zero would be a false statement about an empty store instead of a true
one about an unreadable one.

---

## Supabase — your project's table editor

Open the project → **Table editor** → the table named by `SUPABASE_TABLE`. Rows
are visible directly, embeddings included. It is the most transparent of the
hosted options: what you see is what the adapter wrote.

---

## After a benchmark run: cleaning up

**Arena contestants run in throwaway homes**, so a race never touches
`.waku/state.db`. But the *hosted* backends have no such isolation — Mem0 and Zep
write to your real account under user `waku`, and those memories persist after
the run.

Two ways to keep benchmark data out of your real account:

1. **Point the arena somewhere else** — set `MEM0_USER_ID=waku-bench` and
   `ZEP_USER_ID=waku-bench` before a race, and your day-to-day memories stay in
   the `waku` partition.
2. **Delete the benchmark user** in each console when you are done.

Do this before you film. A store card showing your real memories will put your
address and your colleagues' names on screen — the Arena reads live data by
design, and it cannot know which of it you would rather not publish.

---

## What to compare, once you are looking at all of them

The interesting question is not "who scored higher". It is **what each one chose
to keep**:

- Give them all the same sentence and compare the stored form. Mem0 rewrites it,
  Zep decomposes it into a graph, sqlite keeps it nearly verbatim.
- Say something that contradicts an earlier fact, then look at what happened to
  the old one. Row stores leave it sitting there, equally retrievable. Zep marks
  it superseded.
- Then ask the question in a different language, or in different words, and see
  which store still finds it.

That last one is where Waku's own FTS5 store is weakest and the vector-backed
services are strongest — worth knowing before you claim anything on camera.
