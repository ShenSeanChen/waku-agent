# Four memory backends, each on its own terms

Four scripts. Each one uses a single memory product the way its own docs tell
you to, with **no waku, no framework, and no shared interface**. Roughly 60
lines each.

They exist because waku's Arena tab does the opposite. The Arena drives all of
these through one `FactStore` contract so it can score them fairly — and that
contract is exactly what hides what makes each one different. The clearest
case: to satisfy "a write must always store", the Arena calls mem0's `add()`
with `infer=False`, which switches off the one feature mem0 is known for.

So: read these first, race them second. The Arena's number means something
quite different once you have seen what it flattened.

## The five beats, identical in all four

Every file does the same things in the same order, so you can put two of them
side by side:

1. **connect** — the platform's own idiom, not an adapter
2. **write three sentences** — the same three, everywhere
3. **read back raw** — what the store *kept*, next to what you *said*
4. **ask three ways** — exact phrase, paraphrase, and the same question in 中文
5. **where to look** — the console URL, or an honest "there is no console"

The three sentences are chosen so the **third contradicts the second**:

```
I met Yuki at the Lisbon AI meetup in March. She runs a robotics startup.
Our product launch is scheduled for May.
Actually, the launch moved to June.
```

Watch what each store does with that. It is the single most revealing thing in
the folder, and it is where they stop being interchangeable.

## Running them

```bash
uv pip install -e '.[arena]'
python examples/memory-native/langmem_native.py
```

Each script loads your repo-root `.env`, so no exports are needed. Each writes
to its own quickstart partition (`quickstart-mem0`, `quickstart-zep`, …), never
to the `waku` partition your real assistant uses.

| file | needs | writes to |
|---|---|---|
| `langmem_native.py` | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | RAM. Nothing survives the process. |
| `mem0_native.py` | `MEM0_API_KEY` | your mem0 account, user `quickstart-mem0` |
| `zep_native.py` | `ZEP_API_KEY` | your Zep project, user `quickstart-zep` |
| `supabase_native.py` | `SUPABASE_URL`, `SUPABASE_KEY`, `OPENAI_API_KEY` | a table you create (SQL in the file header) |

## What each one is actually for

- **mem0** — decides what is worth remembering. You say a sentence, it stores a
  different one. Step 3 is the whole product.
- **Zep** — not rows, a temporal knowledge graph with validity intervals. When
  you contradict yourself the old edge is marked invalid *at a point in time*
  rather than out-ranked. Ingestion is async; the script waits, and explains
  why skipping that wait makes Zep look like it forgot.
- **LangMem** — a library, not a service. No dashboard, no account, and by
  default no persistence: the store is a dict in your process. Its extractor
  reads the **whole conversation at once**, so it resolves the May→June
  contradiction *before* anything is stored — 3 sentences in, 2 memories out.
- **Supabase pgvector** — the roll-your-own baseline. ~30 lines, real
  embeddings, genuinely good at the paraphrase and the Chinese question. And
  nothing in it ever decides a fact stopped being true, so both launch dates
  sit there as neighbours forever. That gap is the argument for the other three.

## What actually happened when we ran them (2026-08-12)

Same three sentences, same three questions, three different stores. None of
this is from the docs.

**mem0** kept the contradiction as two separate rows and never resolved it:

```
kept : User's product launch is scheduled for May 2026
kept : User's product launch is scheduled for June 2026
```

It also inferred a year we never said. And the same question in three languages
did not get the same answer — English returned June, **中文 returned the
superseded May**:

```
exact      : When is the product launch?   -> ...scheduled for June 2026
chinese    : 发布会是什么时候?              -> ...scheduled for May 2026
```

**LangMem** resolved the contradiction before storing anything — three
sentences in, two memories out:

```
kept : User's product launch is scheduled for June (updated from May - date was moved).
```

**Zep** did the thing it is built for, and marked the old fact invalid at a
timestamp rather than out-ranking it:

```
The product launch is scheduled for May.  ->  INVALID from 2026-08-12T21:24:47Z
The launch moved to June.                 ->  still valid
```

Worth noting: a plain `graph.search` still returned the May fact for "When is
the product launch?". The invalidity lives on the edge, so you have to read the
interval — retrieval alone will hand you a superseded fact with a straight face.

### Solved: the "foreign entities" were never a leak

Earlier runs kept producing entities nobody had mentioned — `CTR`, `brand
deal`, `contract`, `rough cut`, `retention rate` — in brand-new Zep users told
nothing but the three sentences above. The first guess was cross-user bleed.
Then the vendor's onboarding sandbox looked like the source. Both were wrong.

We deleted **every user in the project** and verified zero remained. A fresh
user, on a verifiably empty project, came back with:

```
node : CTR
node : brand deal -- A brand deal has a priority associated with May.
node : retention rate -- Retention rate is the percentage of viewers watching until...
node : rough cut -- A rough cut is an unpolished initial video edit.
```

Nothing was left to leak from. **It is the project-wide ontology.** Zep's
onboarding wizard sets one — visible on the project page under *Project Wide
Customization*, and ticked off as "Set your ontology" in Getting Started — and
every user in that project inherits those entity definitions forever.

This is not a bug and not a leak. It is a configured schema doing exactly what
it says. But three consequences matter if you are benchmarking:

- **Deleting users does not clean a Zep project.** The ontology outlives them.
- A clean comparison needs the ontology reset, or a **fresh project**.
- The ontology **shapes what the graph is willing to learn.** In the run above,
  `Our product launch is scheduled for May` and `Actually, the launch moved to
  June` produced a `May` node and no `June` node at all — and every question
  about the launch still answered "May", with the superseded edge never marked.
  Whether that is the ontology constraining extraction or ingestion still
  settling, we have not yet separated.

The general lesson is the one worth filming: **a schema you agreed to once, in
a setup wizard, quietly decides what your agent is capable of remembering.**

### One open question, stated as open

- **A user given one unrelated sentence reported zero episodes** several
  minutes after a `graph.add()` that returned without error. Not explained.

### "Ingested" and "queryable" are different events

Both hosted stores made us learn this the hard way, and neither reports the
event you actually care about.

- **Zep** exposes `processed` on an episode. It is necessary and not
  sufficient: on a clean project every episode reported processed while the
  launch facts had produced no nodes at all, so the graph answered *"when is
  the launch?"* with the Lisbon meetup. A benchmark stopping there publishes
  "Zep forgot a fact" about a fact it was mid-way through filing.
- **mem0** has no flag at all, and `add()` returns early. The obvious fix —
  wait for `len(FACTS)` rows — is also wrong, because an inferring store
  decides for itself how many memories your sentences become. Three sentences
  became four here, and waiting for three returned the instant *before* "moved
  to June" landed.

Both scripts now wait for the count to **stop changing** rather than for a
signal or a target. If you benchmark any store that infers, do the same, or
you are timing your own race conditions and calling it recall.

## Adding another one

Keep the five beats and the same three sentences, or it stops being
comparable. And per `CLAUDE.md`: no new default dependencies, nothing in
`waku/` may import from here, `make gate` must not depend on it, and put the
SDK version you verified against in the file header — these libraries move fast
enough that a silently rotted example is worse than no example.
