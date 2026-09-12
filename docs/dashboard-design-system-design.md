# Dashboard design system — the Waku Memory look

Status: PROPOSED. PR 1 of 3 (foundation) is specified here in full; PRs 2 and 3
are sketched in §9 and get their own section of this doc before they start.
Scope: `waku/ops/static/` only. No Python, no API, no terminal UI.

## 1. What we're changing and why

The dashboard and the Waku Memory console are two faces of one product family,
and today they look like two products: violet accent, system font, 6–10px
radii and drop shadows here; bone and charcoal, one amber, 2px radius and no
shadows there. The goal is that the dashboard looks like it was built from the
same design system as the console, because it is.

The work is split into three PRs, each built on the one before:

1. **Foundation** (this doc) — colour, type, radius, depth, motion and the
   control contract come from Waku Memory. Layout does not move.
2. **Shell** — sidebar, page header and chat dock re-laid-out after the console.
3. **Views** — each tab re-laid-out after the matching console pattern.

## 2. Decision: vendor Waku Memory's two design files, byte for byte

Waku Memory keeps every raw value in one plain-CSS file, `tokens.css`, and
every control's shared behaviour (shape, one duration, focus ring, pressed,
disabled) in a second, `controls.css`. Neither needs a framework or a build
step. We copy both unchanged.

Considered and rejected:

- **Link them from the Memory website.** Always in sync, but the dashboard
  would need the network to look right, and a hidden network call is against
  this repo's rules.
- **Transcribe the values into our own variables.** Smallest diff, but it
  makes two design systems that drift apart on the first change.

Cost of the chosen route: when Memory changes a token, someone copies the file
again. §7's hash check makes a local edit to a vendored file fail CI, so the
copy is only ever replaced, never forked in place.

Source: `ShenSeanChen/waku-memory-frontend` at `03ab1d7`,
`public/design/tokens.css` and `public/design/controls.css`.

## 3. Files

```
waku/ops/static/
  design/
    SOURCE.md        # repo, commit, path and sha256 of each vendored file
    tokens.css       # vendored, unchanged
    controls.css     # vendored, unchanged
    fonts.css        # @font-face for the three faces, local files only
  fonts/
    InstrumentSans-*.woff2
    JetBrainsMono-*.woff2
    PlayfairDisplaySC-400.woff2, PlayfairDisplaySC-700.woff2
    OFL.txt          # all three faces are SIL Open Font License
  index.html         # links fonts.css, tokens.css, controls.css, then style.css
  style.css          # consumes tokens; holds no raw colour
```

`tokens.css` reads its faces from `--font-instrument-sans`,
`--font-jetbrains-mono` and `--font-playfair-sc`, which next/font supplies in
the console. `fonts.css` supplies them here, on `:root`, from the local files.
Font files ship in the wheel because `waku/` is packaged whole; nothing is
fetched at runtime.

## 4. Old variable names point at tokens

`style.css` keeps its short names for this PR so the 18 inline
`style="…var(--x)…"` attributes in `js/` keep working untouched. Its `:root`
block and dark-mode block are replaced by one alias block with no values in
it:

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

`--accent` and `--bad` are no longer defined in `style.css` at all: the names
collide with tokens, and the token wins. Dark mode comes from `tokens.css`,
which follows the OS and also honours `data-theme` for PR 2's toggle.

## 5. What changes on screen

**Colour.**
- Amber is a surface, not a text colour. The 49 places in `style.css` and 3
  in `js/` that write `color:var(--accent)` switch to `--accent-fg`; text on
  an amber fill uses `--accent-ink`.
- Every hard-coded colour goes. The warning amber (`#c8951f` family) collides
  with the new accent and becomes `--warn`. The three channel-tag hues become
  one neutral tag distinguished by its word, because the system allows one hue
  per screen. The dark SQL box becomes `--surface-sunk` with ink text. `#fff`
  on accent becomes `--accent-ink`. `#4c9aff` in `models.js` becomes a token.
- The gate split bar is a chart, not a warning: its two segments take
  `--chart-1` and `--chart-3` from the one-hue ramp, and the gather wave bars
  take `--chart-1`.
- The user's chat bubble stops being an amber block: `--surface-raised`
  ground, `--rule` border, ink text.
- Scrollbars use `--scrollbar-thumb`.

**Type.**
- The body is Instrument Sans at the console's product size (15px/1.72).
  Numbers, ids and labels are JetBrains Mono; the page title is Playfair
  Display SC.
- Font sizes snap to the six-step scale (12 / 13 / 15 / 20 / 27 / 36). The
  current 9–12.5px sizes become 12, 13–14 become 13.
- Only weights 400 and 500. Every 600/650/700 becomes 500.
- Section labels ("SYSTEM", "RETRIEVAL GATE", table headers) are uppercase
  mono at the one tracking value, `--tracking-label`.

**Shape and depth.**
- Every rectangle takes `--radius` (2px). Chips and badges take `--shape-chip`,
  status dots `--shape-circle`. This also defines the `--radius` that
  `style.css` already reads once but never set.
- Every shadow is removed. Layers are separated by a 1px rule; overlays get
  `--scrim`.

**Controls.**
- Buttons never fill. A button that is accent-filled today (Send, Save)
  becomes primary: paper ground, `--rule-hard` border, ink label. Others are
  secondary (transparent, `--rule`, muted label), text-only buttons are
  tertiary, delete buttons are destructive. Hover moves the border; keyboard
  focus draws the 2px amber ring from `controls.css`.
- Fields: transparent ground, `--input` border, amber border on focus.
- Disabled is a colour, never an opacity.

**Motion.** Transitions move colour, background and border only, at
`--motion-fast`. The architecture diagram's live animation and the chat caret
stay: they report state, they don't decorate. `prefers-reduced-motion` stops
them.

## 6. Reaching the controls: stamp `data-slot`

`controls.css` targets `[data-slot="button"]`, `[data-slot="input"]` and so
on. The views write about 70 controls as HTML strings across ten files, and
every future view would have to remember the attribute too. Instead of
editing each string, `util.js` gains one function that stamps it on native
controls:

| Element | `data-slot` |
|---|---|
| `button` | `button` |
| `input` (text-like), `textarea` | `input`, `textarea` |
| `select` | `select-trigger` |
| `input[type=checkbox]` / `[type=radio]` | `checkbox` / `radio-group-item` |
| existing pill / chip classes | `badge` |

It runs once on load and from a `MutationObserver` on `document.body`, because
views are rebuilt every 5 s and modals are appended to the body. An element
that already has a `data-slot` is left alone.

## 7. Tests

New `evals/deterministic/test_design_system.py`:

- each vendored file's sha256 equals the one recorded in `design/SOURCE.md`;
- `style.css` and `js/*.js` contain no hex or `rgb()` colour literal (an
  HTML entity such as `&#9662;` is not a colour and is allowed);
- `fonts.css` names no `http` URL, and every file it references exists;
- `index.html` links `fonts.css`, `tokens.css`, `controls.css` before
  `style.css`;
- the stamping function exists and is called (the same static style as
  `test_static_assets.py`, which already checks that every handler is defined).

Existing tests keep passing unchanged, including `test_brand_mark.py`: the
mark's fills are already the Memory ink values, and the sidebar mask paints in
`--ink`, which resolves to the same colour.

## 8. What does not change

- Layout, routes, hash keys, every feature and every behaviour hook listed in
  `static/README.md` (ids, `data-v`, `.chatlog`, the `archSVG` structure).
- `archSVG` stays byte-frozen. It is recoloured only through the variables it
  already reads.
- The terminal UI, the OAuth callback page, `docs/architecture.html`.

Verification: `make gate`, `make lint`, `pytest evals/deterministic`, then a
copy on port 7778: hard reload, every tab in light and dark, zero console
errors. Before/after screenshots go in the PR.

## 9. Later PRs, sketched

**PR 2 — shell.** The sidebar becomes the console's nav rail: uppercase mono
items, a 2px amber rule on the active item, faint tabular counts, a theme
toggle in the bottom block (stored under `waku-theme`, the key the console
uses). To decide: whether the rail keeps its drag-to-resize, where the
`provider · model` line goes, the three group headings (the rail has none),
and how the chat dock (which the console has no equivalent of) is framed.

**PR 3 — views.** Each tab adopts its closest console pattern: stat band,
table, list rows, card grid, dialog, notice. To decide, because the console
has no equivalent: the architecture and graph diagrams, the gate split bar,
the model-race live columns and cost/quality scatter, the memory-race outcome
colours, and sub-tabs.
