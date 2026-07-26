# pi — the whole walkthrough  /  完整讲解

**The one file to film from.** Follows the chart left→right, top→bottom. For every
box: what it is, **where it actually lives on disk**, and the line to say.

Pinned: **pi 0.82.0** · source read at `~/Developer/pi` @ `24e5cc0` (2026-07-24) ·
demos verified live 2026-07-25. Every `[tested]` ran on this machine.

The one question the video answers: **who owns the context — the vendor, or you?**
pi's answer: *you do.*

Chart: `~/Developer/Excalidraw/26.07.25 pi agent.excalidraw`

---

## 0 · The three scopes — where anything you add can live

This table is the mental model for the whole second half. **Where a thing lives
decides who gets it.**

| Scope | Path | Who gets it |
|---|---|---|
| **You**, everywhere | `~/.pi/agent/` | just you, in every project |
| **This repo** | `<repo>/.pi/` · `<repo>/.agents/skills/` | anyone who clones it |
| **The world** | a package → `pi install npm:… / git:…` | anyone who installs it |

```
~/.pi/agent/
├── sessions/<cwd-slug>/<timestamp>_<uuid>.jsonl   the conversation tree
├── skills/                                        your global skills
├── settings.json · auth.json · trust.json         config, keys, trusted dirs
└── models-store.json                              the local price/model table
```

**中文:** 东西放在哪,决定谁能用到:`~/.pi/agent/` 只有你、`<repo>/.pi/` 全队、
package 全世界。作用域就是这一课。

---

## 1 · The spine — walk the chart

### FACES — one brain, four costumes

| Command | What it is |
|---|---|
| `pi` | **TUI** — interactive app in your terminal |
| `pi -p "task"` | **CLI** — answers once, exits. How scripts embed it |
| `pi --mode json` | **event stream** on stdout, one JSON per line ← *waku plugs in here* |
| `pi --mode rpc` | long-lived pipe for other programs |

All four consume the **same event stream** — the loop never knows who's watching.

> **Say:** "Claude Code has the same three faces — `claude` is a TUI, `claude -p`
> is a CLI, the desktop app is a GUI. The interface is a costume, not the agent."

Two-command demo you can run on camera:
```bash
claude -p "Reply with exactly: waku waku"   # answers, dies
claude    "Reply with exactly: waku waku"   # banner, state, waits for you
```

### CONTEXT — the whole state

`user prompt` + `session so far` + `AGENTS.md` + a **system prompt under 1k tokens**
= one plain-JSON message list.

- Where: `~/Developer/pi/packages/coding-agent/src/core/system-prompt.ts` (162 lines)
- **AGENTS.md ≠ SOUL.md.** `AGENTS.md` belongs to the *repo* (conventions, test
  commands — your `CLAUDE.md` is exactly this, and pi reads either). `SOUL.md`
  belongs to the *agent* (identity, personality). pi has **no** SOUL.md — it
  doesn't want a personality, it's a contractor. pi walks *up* the directory tree
  and concatenates every `AGENTS.md` it finds — that's project scoping, not identity.

> **Say:** "This is the whole state of the system. Nothing enters it that you
> didn't put there. And because it's plain JSON, a different brain can pick up
> the conversation mid-flight."

### THE LOOP — `agent-loop.ts`, 792 lines

```bash
wc -l ~/Developer/pi/packages/agent/src/agent-loop.ts     # → 792
```

The three chart symbols, each a real place in the code:

**◇ `tools?`** — [`agent-loop.ts:203`](../../pi/packages/agent/src/agent-loop.ts)
```typescript
const toolCalls = message.content.filter((c) => c.type === "toolCall");
if (toolCalls.length > 0) { … }
```
That's the entire diamond. The model's reply either contains tool requests or it
doesn't. **No → the turn is over → REPLY.** Yes → run them, feed results back,
ask again. *This is the loop's only exit condition.*

**□ `read · write · edit · bash`** — four files:
```
~/Developer/pi/packages/coding-agent/src/core/tools/
├── read.ts  write.ts  edit.ts  bash.ts      ← the four built-ins
└── find.ts  grep.ts   ls.ts                 ← opt-in extras
```
A tool = a name + a description + an argument schema + a function. The model never
touches your disk; it can only *ask* for one of these. Run in **parallel** by
default (`agent-loop.ts:425`), sequentially when a tool demands it.

**◇ `allowed?`** — the extension gate.
[`extensions/types.ts:1065`](../../pi/packages/coding-agent/src/core/extensions/types.ts):
```typescript
export interface ToolCallEventResult {
  block?: boolean;
  reason?: string;
}
```
Before any tool runs, pi asks every loaded extension *"this is about to happen —
object?"* If one returns `{block: true, reason: "…"}`, `runner.ts:932` stops it.
**With no extensions loaded, nothing ever blocks** — the diamond is *your*
insertion point. That's why the chart's arrow runs from EXTENSIONS to this diamond.

**"blocked → returned as a tool result"** — the `reason` string is fed back **in
the same slot a successful tool result would occupy.** To the model it looks like
any other tool output, so it adapts instead of crashing. That's why `reason` sits
next to `block`: a boolean can only stop; a string can *teach*.

> **Say:** "The refusal comes back as a tool result. The model reads *why* and
> works around it. Most harnesses would just crash."

### TWO EXITS

- **REPLY** (green) — the model stopped asking for tools.
- **SESSION** (teal) — a **tree, not a log**. One JSONL file, every line
  `{id, parentId, message}`. `/fork` branches **in place**, `/tree` time-travels,
  and even *switching model* is a node.
  `~/.pi/agent/sessions/<cwd-slug>/<timestamp>_<uuid>.jsonl`

> **Say:** "git for conversations."

---

## 2 · The band — four ways to make pi strong, cheapest context first

**The ordering is the argument.**

### ① `bash` + a README  (the thing that replaces MCP)

pi's model already has `bash`, and bash runs **every program on your machine**. So
the capability is already there — the only missing piece is *knowing how to use
it*. Put a README next to the tool; the model reads it **when it needs it**.

- **Who calls it? The model does**, through the `bash` tool, inside pi's loop.
- **Cost:** an MCP server pastes every tool's JSON schema into context at startup —
  ~13.7k tokens for a big one, **every turn, used or not.** A CLI + README costs
  **0 tokens until the moment of use.**
- Live example already on this machine: `~/.pi/agent/skills/pi-skills/brave-search/`
  — a CLI plus a `SKILL.md`. Web search, no server, no protocol, no per-turn tax.

### ② SKILLS — knowledge the model *reads*

A folder + `SKILL.md`. **Markdown, no code.** At startup pi reads only each
skill's *name and description*; the body loads on demand when a task matches —
**progressive disclosure**, straight from pi's docs. Force it with `/skill:name`.

Lives in: `~/.pi/agent/skills/` (you) · `<repo>/.pi/skills/` or `.agents/skills/` (team)

### ③ EXTENSIONS — a verb the model *calls*

**One TypeScript file**, hot-loaded, ~30 lifecycle hooks. Two jobs:
**add a verb** (`registerTool`) or **guard a verb** (`on("tool_call")`).
The only layer that can change the loop's behavior.

Lives in: `~/.pi/agent/extensions/*.ts` · `<repo>/.pi/extensions/*.ts`
79 shipped examples: `~/Developer/pi/packages/coding-agent/examples/extensions/`

> **Say:** "Ten lines is a working permission system."

### ④ PACKAGES — the shipping box

An npm or git bundle of skills + extensions + prompts + themes, declared under a
`"pi"` key in `package.json`. `pi install npm:… / git:…`. 2,100+ published.

> **Say:** "Extension = the app. Package = the App Store listing."

**The three contrasts to land:**
1. **Skill vs extension** — a recipe the model reads and drives bash itself, vs a
   real typed tool in its tool list running your code.
2. **Extension vs package** — you *write* extensions; you *ship* packages.
3. **Extension vs MCP** — same idea (a tool the model calls), but local, in-process,
   only *your* tools in context, and it can also guard calls and draw UI. No server,
   no per-turn schema tax.

---

## 3 · Refused, on purpose

| Refused | Use instead |
|---|---|
| MCP | any CLI + its README |
| sub-agents | spawn more pi's (tmux — or waku's `delegate_task`) |
| plan mode / todos | `PLAN.md`, `TODO.md` — files you can open |
| permissions | run it in a container |
| **memory · evals** | **the orchestrator's job** — pi's "memory" is just the raw session |

> **Say:** "The refusals aren't holes, they're redirects. And every refusal became
> somebody else's package — that's why there are 2,100 of them."

---

## 4 · waku × pi — the collab thesis

```
waku loop → delegate_task → subprocess → pi -p --mode json
     pi's events → the arena card (live mini-terminal)
     pi's tokens → waku's usage ledger (coding runs aren't free)
```

Wiring: [`waku/tools/experimental.py`](../waku/tools/experimental.py) — every
delegated pi run inherits this repo's own extensions and skills:

```python
def _project_pi_flags() -> list[str]:
    root = Path(__file__).resolve().parents[2]
    flags = []
    for ext in sorted((root / ".pi" / "extensions").glob("*.ts")):
        flags += ["--extension", str(ext)]
    skills = root / ".agents" / "skills"
    if skills.is_dir():
        flags += ["--skill", str(skills)]
    return flags
```

> **Say:** "pi refused to build sub-agents — which is exactly what makes it
> embeddable as one. Memory and evals belong to the orchestrator; tools belong to
> the specialist."

---

## 5 · The live demo — a Trainer's journey  /  养成大师

The four ways to add power, as a rookie→Champion arc. PokeAPI is public (no key).

**Pre-flight, off camera:**
```bash
cd ~/Developer/waku-agent
git stash                       # clean repo — see the first gotcha
set -a; source .env; set +a
```

| Stage | Beat | Layer | Lives in |
|---|---|---|---|
| 0 | Route 1, barehanded | **ANY CLI** | nothing — bash runs it |
| 1 | Prof. Oak's Pokedex | **SKILL** | `.agents/skills/pokedex/` |
| 2 | Moves + gym rules | **EXTENSION** | `.pi/extensions/pokemon-battle.ts` |
| 3 | Earning the Badge | **PACKAGE** | `pi-pokedex/` |
| 4 | The League | **waku × pi** | `delegate_task` |

### Stage 0 — bash is already a tool
```bash
curl -s https://pokeapi.co/api/v2/pokemon/pikachu | head -c 200
```

### Stage 1 — SKILL  **[tested]**
```bash
./.agents/skills/pokedex/pokedex.sh charizard
#  Charizard #6 · fire, flying · HP 78 / Atk 84 / Def 78 / SpA 109 / SpD 85 / Spd 100

pi --skill .agents/skills/pokedex --provider anthropic --model claude-haiku-4-5 \
   -a --no-session -p "Use the pokedex skill to look up snorlax and report its base stats."
#  Snorlax (#143) — HP 160, Atk 110, Def 65, Spd 30 …
```
The `SKILL.md` is the instruction card; the `.sh` is the tool it runs. **Both
together are the skill.**

### Stage 2 — EXTENSION  **[tested]**
```bash
# 2a — add a verb
pi -e .pi/extensions/pokemon-battle.ts --provider anthropic --model claude-haiku-4-5 \
   -a --no-session -p "Call type_matchup with attacker=fire, then say what fire beats."
#  → Fire is super-effective against grass, ice, bug, and steel.

# 2b — guard a verb (throwaway dir with a dummy .waku)
pi -e .pi/extensions/pokemon-battle.ts … -p "Run: rm -rf .waku"
#  → "…blocked by a safety guard. The .waku directory is protected…"
#  → .waku SURVIVED. The refusal came back as a tool result and the model
#    adapted — offered to back up / narrow the path — instead of crashing.
```
**That is the `allowed?` diamond, live.**

### Stage 3 — PACKAGE  **[tested]**
```bash
# -ne disables auto-discovery: this repo also has loose copies in .pi/ and
# .agents/, and loading both registers type_matchup twice → hard error.
pi -ne -e ./pi-pokedex --provider anthropic --model claude-haiku-4-5 -a --no-session \
   -p "What single type beats Charizard? Use the pokedex, then type_matchup. One word."
#  → Rock   (pokedex: Fire/Flying → type_matchup → Rock beats both)

pi install ./pi-pokedex   &&   pi list   &&   pi remove ./pi-pokedex
```
Skill + extension **compose**. The collision is itself the lesson: skills warn and
keep the first; extension tools **hard-error** — two copies of a verb is ambiguous,
so pi refuses rather than guess.

### Stage 4 — waku × pi
```bash
WAKU_EXPERIMENTAL=1 make run        # + make dashboard (:7777)
# ask waku: "delegate to pi: what beats Charizard? use the pokedex + type_matchup"
```
Watch the arena card stream the sub-agent's tools live.

> **Say:** "We upgraded the contractor without touching the orchestrator's brain."

---

## 6 · Gotchas — the best teaching moments

- **pi runs the model raw, and it can wander.** On one run, haiku ignored "what
  does fire beat?" and ran `git add` instead, reporting "Staged, ready to commit."
  Why: pi loads `AGENTS.md`/`CLAUDE.md` from the repo, and waku's says *"commit
  every milestone."* Seeing a dirty repo, the model followed the ambient rule over
  the trivial prompt. 3 of 4 runs were correct. **Clean repo + explicit prompt.**
- **The knowledge-cutoff moment.** A Gemini session read `gpt-5.6` in pi's *own
  docs* and concluded it was "a fictional example in a simulated timeline" — its
  training cutoff arguing with the evidence in front of it. No harness padded it.
  *"Claude Code makes every model look smart because the harness carries it.
  pi is a microscope."*
- **Extension tool names must be unique** — loose files + package together = hard
  error. Use `-ne`, or one scope at a time.

## 7 · The five punchlines

1. "The context window is the entire state — nothing enters it you didn't put there."
2. "MCP pastes its schema every turn; a CLI + README costs nothing until you use it."
3. "Ten lines is a working permission system."
4. "Extension = the app; package = the App Store listing."
5. "Memory and evals belong to the orchestrator; tools belong to the specialist."

## 8 · The on-camera code paths

```bash
wc -l ~/Developer/pi/packages/agent/src/agent-loop.ts            # 792
ls    ~/Developer/pi/packages/coding-agent/src/core/tools/       # the four
ls    ~/Developer/pi/packages/coding-agent/examples/extensions/  # 79 examples
ls    ~/.pi/agent/sessions/                                      # your own tree
```

---

*Sources: pi source read locally @ `24e5cc0` · [mariozechner.at, "What I learned
building a minimal coding agent"](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)
· [earendil-works/pi](https://github.com/earendil-works/pi) · live tests on this
machine, pi 0.82.0, 2026-07-24/25 · chart prompt:
[whiteboards/pi-chart-prompt.md](whiteboards/pi-chart-prompt.md) · @ShenSeanChen*
