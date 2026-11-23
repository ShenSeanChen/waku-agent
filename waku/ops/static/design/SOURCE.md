# Where the design files come from

The dashboard uses the Waku Memory design system. Do not edit the copied
files here. Change them in Waku Memory, then run
`python scripts/sync_design.py <path to waku-memory-frontend>`.

## Copied unchanged

From `ShenSeanChen/waku-memory-frontend`, `public/design/`, as of commit
`03ab1d7` (the last commit that changed them).
`evals/deterministic/test_design_system.py` checks these hashes.

```
89f2150e9e54292fc1d8a6c190a57124a051b9c4f81bd33cd8c9b22be5686963  tokens.css
6dc594e673120f16d0d284e01e722e2f856c529520a3c6cc7d9c4fff99d596f0  controls.css
```

## Copied by hand

`type.css` holds values Waku Memory keeps in `app/globals.css` rather than in
`tokens.css`: the type scale (`--text-*`), the two weights
(`--font-weight-*`), the line heights (`--leading-*`) and the field border
(`--input`). They were copied from `app/globals.css` at `03ab1d7`. Check them
against that file when you sync.

## Fonts

`../fonts/` holds the Latin subsets Waku Memory loads through next/font, from
Google Fonts. Each face is under the SIL Open Font License; its license is
next to it as `OFL-<face>.txt`.

| File | Face |
|---|---|
| `InstrumentSans-var.woff2` | Instrument Sans, variable, 400–700 |
| `JetBrainsMono-var.woff2` | JetBrains Mono, variable, 100–800 |
| `PlayfairDisplaySC-400.woff2` | Playfair Display SC, 400 |
