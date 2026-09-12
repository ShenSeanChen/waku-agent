# Dashboard design system — the Waku Memory look

Status: PROPOSED. PR 1 (foundation) is specified in full. PRs 2 and 3 are
sketched in §10; each gets a full section here before its code starts.
Scope: `waku/ops/static/` only. No Python, no API, no terminal UI.

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
`app/globals.css`: the type scale, the two weights, and the field border.
They are nine values. `type.css` holds them under the same names, and
`SOURCE.md` records the file and lines they come from:

| Token | Value |
|---|---|
| `--text-xs` / `--text-sm` / `--text-base` | 12px / 13px / 15px |
| `--text-lg` / `--text-xl` / `--text-2xl` | 20px / 27px / 36px |
| `--font-weight-normal` / `--font-weight-medium` | 400 / 500 |
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
    PlayfairDisplaySC-400.woff2, PlayfairDisplaySC-700.woff2
    OFL.txt          # all three faces use the SIL Open Font License
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
  (49 in `style.css`, 3 in `js/`) becomes `--accent-fg`. Text on an amber
  fill uses `--accent-ink`.
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
- The body is Instrument Sans at the console's size, 15px with 1.72 line
  height. Numbers, ids and labels use JetBrains Mono. The page title uses
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

**PR 2 — shell.** The sidebar becomes the console's nav rail: uppercase mono
items, a 2px amber rule on the active item, faint counts, and a theme toggle
in the bottom block. The toggle is stored under `waku-theme`, the same key the
console uses. To decide: whether the rail keeps drag-to-resize, where the
`provider · model` line goes, what happens to the three group headings (the
rail has none), and how the chat dock is framed (the console has no chat
dock).

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
