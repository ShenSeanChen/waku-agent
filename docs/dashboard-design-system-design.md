# Dashboard design system — the Waku Memory look

Status: PR 1 (foundation, #189) and PR 2 (shell, #190) are built and in
review; PR 2 is specified in §11. PR 3 (views) is specified in §12, from
the decisions recorded in §10.
Scope: `waku/ops/static/`, plus one line in `dashboard.py` so the fonts are
served as `font/woff2`. No API change, no terminal UI.

## 1. Goal

The dashboard and the Waku Memory console belong to the same product family,
but today they look like two products. The dashboard uses a violet accent, the
system font, 6–10px corners and drop shadows. The console uses bone and
charcoal, one amber accent, 2px corners and no shadows.

After this work:

- The dashboard is built from the console's design system: its tokens, its
  controls, and its primitives.
- Changing one token changes the whole dashboard. Colour, type, corners and
  depth do this after PR 1. Spacing does it after PR 3.
- New screens follow the system by default. A written rule tells an agent what
  to use, and a test fails when a screen writes its own value.

## 2. Plan

Three PRs, each built on the one before:

1. **Foundation** — tokens, fonts and controls come from Waku Memory. Every
   screen changes colour, type, corners and buttons at once. Layout does not
   move. Adds the agent rules and the first checks.
2. **Shell** — the sidebar, page header and chat dock are rebuilt after the
   console. Adds the light/dark toggle.
3. **Views** — each tab is rebuilt after the closest console pattern. The
   primitives are built here, spacing moves onto tokens, and the old variable
   names are removed.

Each PR opens as soon as it is done. The next one is branched from it and
does not wait for review. The open design questions for PRs 2 and 3 are
decided together, from one mockup, before PR 2 starts.

## 3. Decision: vendor Waku Memory's two design files, byte for byte

Waku Memory keeps every raw value in one plain-CSS file, `tokens.css`, and the
shared behaviour of every control in a second one, `controls.css`. That covers
shape, the one duration, the focus ring, pressed and disabled. Neither file
needs a framework or a build step. We copy both unchanged.

Considered and rejected:

- **Link them from the Memory website.** Always in sync, but the dashboard
  would need the network to render correctly. This repo does not make hidden
  network calls.
- **Copy the values into our own variables.** Smallest diff, but it creates a
  second design system that drifts on the first change.

Cost: when Memory changes a token, someone copies the file again. The hash
check in §8 fails CI on any local edit to a copied file. To change a token,
change it in Waku Memory, then copy the file here.

`scripts/sync_design.py <path to waku-memory-frontend>` does the copy. It
copies both files, and writes the source commit and the new hashes into
`SOURCE.md`, so nobody updates them by hand.

Source: `ShenSeanChen/waku-memory-frontend` at `03ab1d7`,
`public/design/tokens.css` and `public/design/controls.css`.

The console defines a few more values outside those files, in
`app/globals.css`: the type scale, the two weights, the line heights, and
the field border. They are twelve values. `type.css` holds them under the
same names, and `SOURCE.md` records the file they come from:

| Token | Value |
|---|---|
| `--text-xs` / `--text-sm` / `--text-base` | 12px / 13px / 15px |
| `--text-lg` / `--text-xl` / `--text-2xl` | 20px / 27px / 36px |
| `--font-weight-normal` / `--font-weight-medium` | 400 / 500 |
| `--leading-tight` / `--leading-snug` / `--leading-normal` | 1.12 / 1.3 / 1.72 |
| `--input` | ink at 48% on the ground |

## 4. Files

```
waku/ops/static/
  design/
    SOURCE.md        # repo, commit, path and sha256 of each copied file
    tokens.css       # copied, unchanged
    controls.css     # copied, unchanged
    type.css         # the type scale and weights from Memory's globals.css
    fonts.css        # @font-face for the three faces, local files only
  fonts/
    InstrumentSans-*.woff2
    JetBrainsMono-*.woff2
    PlayfairDisplaySC-400.woff2   # titles only, so one weight
    OFL-*.txt        # one SIL Open Font License per face
  index.html         # links fonts.css, tokens.css, type.css, controls.css, then style.css
  style.css          # reads tokens; holds no raw colour
```

`tokens.css` reads its faces from `--font-instrument-sans`,
`--font-jetbrains-mono` and `--font-playfair-sc`. In the console, next/font
sets these. Here, `fonts.css` sets them on `:root` from the local files. The
font files ship in the wheel, because `waku/` is packaged whole. Nothing is
fetched at runtime.

## 5. Old variable names point at tokens

In PR 1, `style.css` keeps its short names, so the inline `style="…var(--x)…"`
attributes in `js/` keep working without edits. Its `:root` block and
dark-mode block are replaced by one alias block. The block holds no values:

| Old | Becomes |
|---|---|
| `--bg` | `var(--surface-bg)` |
| `--panel` | `var(--surface-paper)` |
| `--line` | `var(--rule)` |
| `--line2` | `var(--rule-hard)` |
| `--ink` / `--ink2` / `--ink3` | `var(--text-ink)` / `var(--text-muted)` / `var(--text-faint)` |
| `--accent-soft`, `--good-soft`, `--bad-soft` | `var(--surface-raised)` |
| `--good` | `var(--ok)` |
| `--mono` | `var(--face-mono)` |

`--accent` and `--bad` are no longer defined in `style.css`. The names are
the same as the tokens, and the token wins. Dark mode comes from `tokens.css`.
It follows the OS, and it also reads `data-theme` for the toggle in PR 2.

The aliases exist only to keep PR 1 small. PR 3 replaces every use with the
token name and deletes the block. Until then, the check in §8 stops new code
from using an old name.

## 6. What changes on screen

**Colour.**
- Amber is a surface colour, not a text colour. Every `color:var(--accent)`
  in `style.css` and `js/` becomes `--accent-fg`. Text on an amber fill uses
  `--accent-ink`.
- Every hard-coded colour is removed. The warning amber (`#c8951f` and
  similar) clashes with the new accent and becomes `--warn`. The three
  channel-tag colours become one neutral tag, told apart by its word, because
  the system allows one hue per screen. The dark SQL box becomes
  `--surface-sunk` with ink text. `#fff` on the accent becomes `--accent-ink`.
  `#4c9aff` in `models.js` becomes a token.
- The gate split bar is a chart, not a warning. Its two segments use
  `--chart-1` and `--chart-3`, and the gather wave bars use `--chart-1`.
- The user's chat bubble is no longer an amber block. It uses a
  `--surface-raised` ground, a `--rule` border and ink text.
- Scrollbars use `--scrollbar-thumb`.

**Type.**
- The body is Instrument Sans at `--text-sm` (13px). The console reads at
  15px, but the dashboard packs more onto a screen, and the approved mockup
  uses 13px. Numbers, ids and labels use JetBrains Mono. The page title uses
  Playfair Display SC.
- Font sizes move onto the six-step scale in §3. Today's 9–12.5px sizes
  become 12px. Today's 13–14px sizes become 13px.
- Only two weights. Every 550, 600, 650 and 700 becomes 500.
- Section labels ("SYSTEM", "RETRIEVAL GATE", table headers) are uppercase
  mono at `--tracking-label`.

**Shape and depth.**
- Every rectangle uses `--radius` (2px). Chips and badges use `--shape-chip`.
  Status dots use `--shape-circle`. This also defines the `--radius` that
  `style.css` reads once today but never sets.
- Every shadow is removed. A 1px rule separates layers. Overlays use
  `--scrim`.

**Controls.**
- Buttons never have a colour fill. A button that is amber-filled today (Send,
  Save) becomes primary: paper ground, `--rule-hard` border, ink label. The
  others are secondary (transparent, `--rule` border, muted label). Text-only
  buttons are tertiary. Delete buttons are destructive. On hover the border
  changes. Keyboard focus draws the 2px amber ring from `controls.css`.
- Fields have a transparent ground and an `--input` border. The border turns
  amber on focus.
- Disabled is shown with colour, never with opacity.

**Motion.** Transitions change colour, background and border only, at
`--motion-fast`. The architecture diagram's live animation and the chat caret
stay, because they show state. `prefers-reduced-motion` stops them.

## 7. Reaching the controls: stamp `data-slot`

`controls.css` styles `[data-slot="button"]`, `[data-slot="input"]` and so
on. The views build about 70 controls as HTML strings across ten files. Every
future view would also have to remember the attribute. So instead of editing
each string, `util.js` gets one function that adds the attribute to native
controls:

| Element | `data-slot` |
|---|---|
| `button` | `button` |
| `input` (text-like), `textarea` | `input`, `textarea` |
| `select` | `select-trigger` |
| `input[type=checkbox]` / `[type=radio]` | `checkbox` / `radio-group-item` |
| existing pill / chip classes | `badge` |

It runs once on load, and again from a `MutationObserver` on `document.body`,
because views are rebuilt every 5 seconds and modals are added to the body. An
element that already has a `data-slot` is left alone.

## 8. Rules and checks

Written rules only work when the agent reads them. Checks work either way. So
each rule is written down once and, where it can be, checked.

**Written.** `waku/ops/static/README.md` gets a section, "Design system",
that says:

- every colour, size, weight, corner, shadow and duration comes from a token;
- the token files in `design/` are copies — change them in Waku Memory, then
  copy them here;
- which of the four button levels to use, and when;
- after PR 3: build screens from the primitives in `js/ui.js`, and ask before
  adding a new one.

`CLAUDE.md` points to that section from its architecture map.

**Checked.** A new test, `evals/deterministic/test_design_system.py`. It reads
`style.css` and the inline styles in `js/*.js`. It follows the same approach
as `test_static_assets.py`.

From PR 1:

- each copied file's sha256 matches the one in `design/SOURCE.md`;
- no hex or `rgb()` colour literal (an HTML entity such as `&#9662;` is not a
  colour and is allowed);
- `font-size` only through `var(--text-*)`;
- `font-weight` only 400, 500, or their tokens;
- `border-radius` only through `--radius` or `--shape-*`;
- no `box-shadow` other than `none` or a `--shadow-*` token;
- no old alias name (§5) outside the alias block and the files that used it
  on the day PR 1 merged — the list is written in the test and only shrinks;
- `fonts.css` has no `http` URL, and every file it names exists;
- `index.html` links the design files before `style.css`;
- the stamping function exists and is called.

From PR 3:

- `padding`, `margin` and `gap` only through `--space-*` or a `calc()` of
  `--spacing`;
- no old alias name anywhere;
- the primitives' class names appear only in `js/ui.js` and `style.css`.

Existing tests pass unchanged, including `test_brand_mark.py`. The mark's
fills already use the Memory ink values, and the sidebar mask paints in
`--ink`, which resolves to the same colour.

## 9. What does not change

- Layout, routes, hash keys, every feature, and every behaviour hook listed in
  `static/README.md` (ids, `data-v`, `.chatlog`, the `archSVG` structure).
- `archSVG` stays byte-frozen. It changes colour only through the variables it
  already reads.
- The 24 provider and connection logos in `logos/`. They keep their brand
  colours.
- The terminal UI, the OAuth callback page, and `docs/architecture.html`.

Verification: `make gate`, `make lint`, `pytest evals/deterministic`. Then run
a copy on port 7778 (`WAKU_DASHBOARD_PORT=7778 uv run --frozen waku
dashboard`), hard-reload, open every tab in light and dark, and confirm zero
console errors. Before and after screenshots go in the PR.

## 10. Later PRs, sketched

**PR 2 — shell.** Decided on 2026-09-13 from the mockup
(`docs/proposals/big-decisions.html`, questions 1–5; the recommended option
each time). Specified in full in §11.

**PR 3 decisions**, from the same mockup:

- Stat tiles become Memory's stat band: one bordered strip, a divider
  between figures, label above, 27px mono number.
- Sub-tabs use Memory's `line` tabs: a 2px ink bar, not amber. Amber marks
  the current page in the rail only.
- The retrieval gate shows two large figures over a 6px bar in the chart
  ramp.
- Model race follows Memory's chart rule: one hue, grade written beside
  each point. Card grades become neutral `value` badges; only solved and
  failed keep `--ok` and `--bad`.
- Explanation boxes become Memory's Notice (raised ground, bottom rule, a
  label mark).
- The architecture and graph diagrams stay frozen and are only recoloured.
- Tables, cards, dialogs and Memory-race outcomes follow Memory's
  primitives as specified in its `components/ui`.

**PR 3 — views and primitives.** Each tab uses its closest console pattern:
stat band, table, list rows, card grid, dialog, notice.

The primitives are built while the tabs are rebuilt, so only the ones a tab
needs get built. Each one keeps the console's name, variants and `data-slot`.
Examples: Card, Badge (with its six statuses), Table, Notice, Dialog, Tabs.
The console builds them with React, Tailwind and radix-ui. The dashboard has
no build step, so each one here is a CSS class that reads tokens, plus a
function in `js/ui.js` that returns its HTML. Interaction uses native
elements, such as `<dialog>` for Dialog. `design/SOURCE.md` records the
console commit each primitive was copied from. Each primitive is accepted by
comparing a screenshot of it next to the console's.

PR 3 also moves every `padding`, `margin` and `gap` onto `--space-*`. Values
off the 4px grid, such as 6px and 10px, snap to the nearest step, so some
elements move by 1–2px. This happens here because every tab is being re-laid
out anyway. Then the alias block from §5 is deleted.

To decide, because the console has no equivalent: the architecture and graph
diagrams, the gate split bar, the model-race live columns and cost/quality
scatter, the memory-race outcome colours, and sub-tabs.

## 11. PR 2 — the shell

Decided from the mockup, questions 1–5. Layout numbers come from Memory's
`components/console/console-nav.tsx`.

**The rail.** The sidebar becomes Memory's nav rail.

- 196px wide on a `--surface-paper` ground, with a 1px `--rule` on its
  right.
- The top block holds the mark, the word WAKU in the mono label style, and
  a tertiary button that collapses the rail.
- Items are mono in normal case, 13px, with no tracking, in
  `--text-muted`. The design owner chose this over Memory's uppercase
  labels on 2026-09-13, because thirteen uppercase labels are hard to scan,
  and asked for Memory's rail to change to match. Items sit one step
  (`--space-6`) in from the group labels, so each label reads as their
  parent.
- The current page gets a 2px `--accent` rule on its left, a
  `--surface-raised` ground and `--text-ink` text.
- Counts sit at the right in `--text-faint`. A count of zero shows nothing,
  as in Memory.
- The three group headings stay, as faint mono labels with no rule above
  them. The dashboard has 13 items; Memory has 5.
- Collapsed, the rail is 56px and each item shows its first letter. The
  full name is in the item's `title` and `aria-label`. The collapsed state
  is not remembered: a rail that reopens closed hides the navigation from
  someone coming back.
- The bottom block holds a GitHub link and the theme toggle. The link has
  no star count, because fetching one would be a network call.

**Removed.** Drag-to-resize on the rail, the hide and reopen buttons, their
`navW` and `navHidden` storage, and the `provider · model` line. The chat
dock's model chip already shows the model and switches it.

**The theme toggle.** One button cycles system → light → dark, like
Memory's. The choice is stored under `waku-theme`, the key Memory uses.
Light and dark set `data-theme` on `<html>`; system removes it, and
`tokens.css` then follows the OS. A short inline script in `<head>` applies
the stored choice before the page paints, so the wrong theme never flashes.
The button shows the state as a 16px icon (contrast, sun, moon) and says it
in its `aria-label` and `title`.

**The page header.** The title stays 20px. It sits on a baseline row that
can hold a count and actions on the right; PR 3 fills those per view. The
`live · updated · path` line stays below it.

**The chat dock.** A `--surface-paper` ground, like the rail, so the two
tool columns frame the page between them. The header reads CHAT in the mono
label style. The dock keeps its resize handle and its collapse button.

**Below 768px.** The rail becomes a row across the top that scrolls
sideways, with the theme toggle fixed at its right end. The chat dock
starts closed and opens from its reopen button.

**Checks.** Added to `test_design_system.py`:

- `index.html` applies the stored theme in `<head>`, before the design
  files load;
- a theme toggle exists and `js/` defines the function it calls;
- every rail link has a `data-short` equal to the first letter of its
  label;
- nothing reads or writes `navW` or `navHidden`, and no `#model` element or
  lookup remains.

## 12. PR 3 — views and primitives

Decided from the mockup, questions 6–11 (§10). This section covers the
parts that apply to every view. The per-view changes are listed in §12.6.

### 12.1 Primitives

`js/ui.js` holds one function per primitive. Each returns an HTML string,
the same way the views already build their markup. Names follow the Memory
console's `components/ui`, with a `ui` prefix, because the `js/` files
share one global scope and a bare `card` or `badge` would collide with
local variables.

| Function | Memory primitive | Drawn as |
|---|---|---|
| `uiCard(body, {title, action, footer, size})` | Card | paper ground, 1px `--rule`, 16px padding (12px at `size: "sm"`); title in the display face |
| `uiBadge(text, variant)` | Badge | 20px chip, mono uppercase at `--tracking-label`; variants `neutral`, `ok`, `warn`, `bad`, `live` (amber edge), `value` (normal case, no tracking) |
| `uiTable(columns, rows, {caption})` | Table | faint mono uppercase header, 8px cells, a `--rule` under each row, `--surface-raised` on hover |
| `uiTabs(items, active)` | Tabs, `line` variant | 13px medium labels in `--text-muted`; the active one in ink with a 2px ink bar |
| `uiNotice(level, html, action)` | Notice | `--surface-raised` ground, a `--rule` under it, a mono level word (`note`, `ok`, `warn`, `failed`) before the message |
| `uiStatBand(items)` | the Overview stat band | one bordered strip, a divider between figures, label above, 27px mono number, faint subline |
| `uiRow(lead, title, meta)` | the recent-writes row | a grid of lead and body, a `--rule` under each row |
| `openDialog(html)` / `closeDialog()` | Dialog | a native `<dialog>`: `--surface-raised`, 1px `--rule`, 32px padding, `--scrim` behind, actions right-aligned in a footer |

`data-slot` is stamped the same way controls are (`stampSlots`), so
`controls.css` reaches every Badge and tab trigger. `/static/README.md`
lists the functions and says which to reach for.

### 12.2 Spacing

Memory's primitives use spacing two ways, and the dashboard follows both:

- **Between things** — blocks, card padding, page and section gaps — only
  the five named steps: `--space-2` 8px, `--space-3` 12px, `--space-4`
  16px, `--space-6` 24px, `--space-8` 32px. A value between two steps goes
  to the one that matches its job: a gap between two related blocks is
  `--space-4`, a group gap `--space-6`.
- **Inside one control** — a badge's padding, the gap between an icon and
  its label — `calc(var(--spacing) * N)` with N of 0.5, 1 or 1.5 (2, 4 or
  6px), the way Memory's Badge uses 2px and its menus 6px.

313 spacing values are raw px today. A 1px nudge becomes 0 or 2px, and
most 10px gaps become 8px or 12px, so some elements move by 1–4px. The page
gutter moves from 40px to `--space-8`.

### 12.3 Old names removed

Every `var(--bg)`, `var(--ink2)` and the rest of §5's aliases becomes the
token it points at, in `style.css` and in `js/`. Then the alias block is
deleted.

### 12.4 Checks

Added to `test_design_system.py`, replacing the PR 1 alias allow-list:

- `padding`, `margin` and `gap` only through a `--space-*` token, a
  `calc()` of `--spacing`, `0` or `auto`;
- no old alias name anywhere, and no alias block;
- `js/ui.js` loads before the views, and each primitive's class names
  (`notice`, `stat-band`, `tabs`, …) appear only in `js/ui.js` and
  `style.css` — a view calls the function instead of writing the markup.

### 12.5 Verification

As for PR 2: the full deterministic suite, lint, a wheel build, and all
13 tabs in light and dark with zero console errors, with before and after
screenshots in the PR.

### 12.6 Per view

From an inventory of every pattern the views emit (file and function in
brackets).

- **Stat tiles → `uiStatBand`.** Overview and Ops headline numbers, and
  Memory's consolidation figures (`views.js`: `overview`, `ops`,
  `memConsolidation`). Memory's three pillars stay cards.
- **Sub-tabs → `uiTabs`.** `subtabBar` in `views.js` (Memory, Tools,
  Database). The arena's sort buttons stay buttons.
- **Explanation boxes → `uiNotice`.** "Memory vs Database", "Database vs
  Memory", the episodic note, the MCP status box, and the connection
  setup box (`views.js`). Plain cards of "How it works." prose stay cards.
- **Tables → `uiTable`.** The shared `table()` helper in `render.js` (ten
  callers), the hand-built tables in `memSemantic`, `memEpisodic`, the
  database views, the memory race and the scoreboard (`compare.js`).
  Markdown tables in chat replies keep their own style.
- **Badges → `uiBadge`.** `.badge`, `.pill`, `.chip`, `.srcpill`,
  `.gwtag`, `.stage`, `.cmp-score`, `.cmp-q`, `.ma-o`, `.ma-test`,
  `.mm-def` and `.conn-secret-state` become one Badge with variants:
  - pass, solved, connected, current, done → `ok`;
  - stale, partial, needs setup → `warn`;
  - fail, failed, invented, error → `bad`;
  - a stage or node that is running → `live`;
  - miss → `neutral` with a dashed edge, like Memory's `observed`;
  - numbers, model names, grades (`8.5 / 10`), costs → `value`;
  - everything else → `neutral`.
- **The retrieval gate → two figures over a thin bar** (§10). `gateSplit`
  in `render.js`, and the graph panel's copy of it in `graph.js`, which
  then uses the same function.
- **The model-race scatter → one hue** (§10). Points take `--chart-1` to
  `--chart-4` by grade, and each label prints the grade and the cost.
- **Dialogs → `openDialog`.** The provider dialog (`models.js`) and the
  connection dialog (`views.js`). Both gain Escape to close and focus on
  open; the connection dialog already had them.
- **Rows → `uiRow`.** The recent-races list (`compare.js`) and the
  gateway inbox (`views.js`).

Left as they are: the architecture and graph diagrams (§10), the chat
dock's menus (`.sessmenu`, `.modelmenu`), and the `<a class="reveal">`
links. The links are about 44 inline actions; making them buttons is a
separate change.

CSS that no JS emits (`.ma-source`, `.ma-warn`, `.chip-c`, `.col-chip`,
`.mm-free`) is deleted rather than moved onto tokens. Unused JS
(`yourModelsCard`, `loadModelList`, `renderCatalog`, `renderCatalogList`,
`modelRow`, `pickTrack`) is out of scope and is listed in the PR.

## 13. PR 4 — components

PR 3 moved every value onto tokens but left some markup hand-written. PR 4
closes that gap. Decided by the design owner on 2026-09-13 from
`docs/proposals/pr4-decisions.html`: **13A, 14A, 15B, 16B**. 15B and 16B
change Waku Memory too, so the two products stay one system.

- **Actions are buttons (13A).** The 45 `<a class="reveal">` actions —
  edit, delete, grade, clear, open in Finder — become Memory's tertiary
  button (mono, uppercase, `--text-muted`, ink on hover; delete is the
  destructive level). They become real `<button>`s, so the keyboard reaches
  them. A link that goes to another tab stays a link and takes Memory's
  link style: `--accent-fg` with a hairline underline that turns amber on
  hover.
- **`uiButton(label, {level, size, onclick, attrs})`.** Memory's four levels
  (primary, secondary, tertiary, destructive) and two sizes. The 27
  buttons with hand-written classes (`save`, `save ghost`, `sessbtn`,
  `msg-copy`, …) call it.
- **Cards (14A).** The 47 hand-written cards call `uiCard`. A card whose
  title is a name — a provider, a connection, a store, a memory pillar —
  sets it in the display face at `--text-base`, as Memory's Card does. A
  title that is code (a tool name such as `create_event`) stays mono.
- **Menus (15B).** The chat history, the model menu and the model picker
  share one menu: `--surface-paper` ground, a `--rule-hard` edge, 12px
  items, `--surface-raised` under the hovered or focused item, and
  `--accent-fg` text on the current one. Escape closes it and focus
  returns to its trigger. Memory's DropdownMenu changes to the same look.
- **Line height (16B).** `--leading-normal` becomes 1.55, not 1.72, in
  Memory and here; the body and every paragraph use it. The other raw
  line heights move to `--leading-snug` (1.3) or `--leading-normal`;
  `line-height: 1` stays for single-line controls.
- **Headline figures.** The stat band and the gate figures are
  `--text-lg` (20px), not `--text-xl` (a PR 3 follow-up). Memory's stat
  band changes to match.

Until the Memory changes land, `type.css` and `SOURCE.md` record that
`--leading-normal` is ahead of `globals.css`.

**Checks.** `test_design_system.py` also fails on: a `class="card"`
outside `ui.js`; a `<button class="save|sessbtn|…">` built by hand; an
`<a class="reveal">`; a `line-height` that is not a `--leading-*` token or
`1`.
