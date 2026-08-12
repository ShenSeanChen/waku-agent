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

## Adding another one

Keep the five beats and the same three sentences, or it stops being
comparable. And per `CLAUDE.md`: no new default dependencies, nothing in
`waku/` may import from here, `make gate` must not depend on it, and put the
SDK version you verified against in the file header — these libraries move fast
enough that a silently rotted example is worse than no example.
