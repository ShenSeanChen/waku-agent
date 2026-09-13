# Dashboard frontend — the map

Plain static files served as-is by `waku/ops/dashboard.py` (a stdlib HTTP
server). **No build step, no framework, no bundler, no dependencies.** Edit these
files to change the UI; edit `dashboard.py` to change the server/API.

- `index.html` — the shell (sidebar nav, `<main>`, chat dock) + the ordered
  `<script>` tags.
- `style.css` — one flat file of rules; every value comes from the tokens in
  `design/` (see "Design system" below).
- `design/`, `fonts/` — the Waku Memory design system and its three fonts.
- `js/` — the app, split by concern (below).

## The files (`js/`), in load order

They are **classic scripts sharing one global scope** — a `function`/`let`/`const`
in one file is visible to all the others. Order matters only in that **`main.js`
runs the bootstrap and must load last**.

| file | what lives here |
|------|-----------------|
| `util.js`    | `esc`, markdown renderer, core globals (`D`, `editing`), `postJSON`, `reveal`, `stampSlots` |
| `theme.js`   | the system / light / dark toggle (`cycleTheme`), stored as `waku-theme` like the Memory console |
| `memory.js`  | inline Memory / SOUL / skill editing actions |
| `models.js`  | `applyModel` (the one `/api/settings` writer), model picker / catalog / pins |
| `render.js`  | formatters + chat card renderers (`stagesRow`/`teleFooter`) + chatlog + streaming + `sendChat` |
| `diagram.js` | `archSVG` (the architecture chart) **and** its live animation (`STAGE`/`hot`/`pollEvents`) |
| `graph.js`   | graph workflows: data-driven topology chart (`graphSVG` from `d.graph.workflows`), the Overview panel (`graphPanel`), and `animateGraphStage` for `graph_*`/`route` events |
| `views.js`   | subtab/db helpers, SQL console, Memory/Tools sub-views, the `VIEWS` router object |
| `compare.js` | the Model arena (`Arena` tab; internals keep the `compare` name) — race one message through several models at once |
| `dock.js`    | chat sessions/history (`loadThreadInto`), model chip, stats toggle |
| `main.js`    | `render`/`refresh` loop, resizers, voice, and the bootstrap (**loads last**) |

Data flows one way: `refresh()` (main.js) fetches `/api/data` into the global
`D`, then `render()` writes `VIEWS[hash](D)` into `#view`. Every mutation
(`applyModel`, `pinModel`, `saveFact`, …) calls `refresh()` when it's done.

## Rules that bite (read before editing)

- **Inline handlers need global names.** Buttons use `onclick="fn()"` in the
  HTML strings the JS generates. `fn` must stay a top-level name in some `js/`
  file. Rename/move a handler and forget its call sites → the button silently
  breaks. `test_static_assets.py` guards this.
- **`archSVG` is byte-frozen — do not rewrite the architecture chart.** It emits
  `data-node="…"`/`data-edge="…"` ids that the `STAGE` map (same file) drives the
  live animation from. If you ever change a node/edge id, change it in both
  places. (Both are in `diagram.js` precisely so they stay together.)
- **The graph chart is data-driven — never hand-edit a topology.** `graphSVG`
  renders `Graph.describe()` served in `/api/data`, so the picture is provably
  what the engine runs (`test_graph_topology_payload.py` pins it). To change the
  chart's shape, change the workflow in `waku/graph/workflows/`. Graph ids are
  namespaced `g-<node>` / `g-<src>-<dst>` so they can never collide with archSVG's.
- **No build step / no framework / no new dependencies.** If you reach for one,
  stop — the whole point is that this reads and runs with nothing installed.
- **No emojis in UI** (project rule). Known pre-existing exception: the `★`/`☆`
  pin stars in `models.js` (typographic dingbats, not colour emoji) — left as-is.

## Design system (read before changing how anything looks)

The dashboard uses the Waku Memory design system. The values live in
`design/`:

- `tokens.css` and `controls.css` are **copies** of Waku Memory's files. Do
  not edit them here — a test checks their hashes. To change a token, change
  it in Waku Memory, then run `python scripts/sync_design.py <path to
  waku-memory-frontend>`. `design/SOURCE.md` says which commit they came from.
- `type.css` holds the type scale, the two weights, the line heights and the
  field border. `fonts.css` loads the three fonts from `fonts/`. Nothing is
  fetched from the network.

When you write CSS here or an inline style in `js/`:

- **Every value comes from a token.** Colour (`--text-*`, `--surface-*`,
  `--rule*`, `--accent*`, `--ok`/`--warn`/`--bad`, `--chart-1…5`), size
  (`--text-xs|sm|base|lg|xl|2xl`), weight (400 or 500), face
  (`--face-sans|mono|display`), corner (`--radius`, or `--shape-chip` for a
  badge and `--shape-circle` for a dot), duration (`--motion-fast`). No
  shadows: separate things with a 1px `--rule`.
- **Amber is a surface, not a text colour.** Amber text uses `--accent-fg`.
  Text on an amber fill uses `--accent-ink`.
- **Buttons never fill.** Four levels: *primary* (the main action — paper
  ground, `--rule-hard` border, ink label: `.save`, Send), *secondary*
  (transparent, `--rule` border, muted label: `.save.ghost`, `.sessbtn`),
  *tertiary* (text only), *destructive* (`--bad` label). Hover moves the
  border. controls.css adds the focus ring, pressed and disabled.
- **Labels** are uppercase `--face-mono` at `--tracking-label`.
- **Controls get `data-slot` automatically** (`stampSlots` in `util.js`).
  Use native `<button>`, `<input>`, `<select>`; don't fake them with `<div>`.
- **Use the token names, not the old short ones** (`--ink2`, `--line`, …).
  The old names are aliases kept only until the views are rebuilt.

`evals/deterministic/test_design_system.py` enforces all of this, except
which button level you pick.

### Primitives (`js/ui.js`)

Build screens from these rather than writing the markup. Each returns an
HTML string and is named after the Waku Memory console's `components/ui`:

| Function | Use it for |
|---|---|
| `uiCard(body, {title, action, footer, size})` | a block of related content |
| `uiBadge(text, variant)` | a status or a value: `neutral`, `ok`, `warn`, `bad`, `live` (running), `miss`, `value` (data, normal case) |
| `uiTable(columns, rows, {caption, empty})` | anything with rows and columns |
| `uiTabs(items)` | switching between parts of one view |
| `uiNotice(level, html, action)` | an explanation or a status message: `note`, `ok`, `warn`, `failed` |
| `uiStatBand(items)` | a view's headline numbers |
| `uiRow(lead, title, meta, {onclick})` | a list of things you can open |
| `openDialog(html, {wide, onClose})` / `closeDialog()` | a task that needs its own space; Escape and the scrim close it |

If none fits, ask before adding one: a new primitive is a design decision.

## Verifying a change (no JS test runner exists)

Frontend logic is not unit-tested; verify in the browser preview:
`make dashboard` (or the preview tool) → hard-reload `localhost:7777` → click the
sidebar tabs and the chat dock → check the console shows **zero errors**. The
Python side (`dashboard.py` endpoints, `_thread_history`, pins, session resume)
*is* covered by `evals/deterministic/`.

**A running server does not pick up Python changes.** Static files here (`.js`,
`.css`, `index.html`) are read from disk on every request, so a hard-reload shows
them. But `dashboard.py` and everything it imports are held in memory — after
pulling or editing backend code, **restart `make dashboard`**, or the page renders
new markup against stale data (e.g. a new Settings panel that shows nothing because
the old route isn't sending its fields).
