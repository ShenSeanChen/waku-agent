# Gotchas

Traps someone already stepped on. This is the only append-only file in the
rulebook. Each entry is one line: a date, a title, the trap and the right
move, and the condition that retires it. The file holds 40 entries at most,
and anyone deletes an entry that no longer holds, in the PR that makes it
false. A gotcha that can become a test should become one.

- **[2026-09-13] The Google sign-in page says the flow completed before Waku has a token** — the browser page appears when Google hands back the one-time code, and Waku exchanges the code afterwards. An `InvalidClientError` after that page means Google rejected the client secret in `.waku/credentials.json`: download the OAuth client's JSON again. _Retire when: `waku connect google` reports a rejected secret in plain words._
- **[2026-09-13] `parseFloat` on a CSS custom property that holds `calc()` returns 0** — `--main-min` is a `calc()` expression, so reading it as a number silently breaks the dock's width limit. Resolve it by layout with a probe element, as `dockMax` in `js/main.js` does. _Retire when: `--main-min` holds a plain length._
- **[2026-09-13] Changing only the URL hash does not reload the dashboard's CSS or JS** — switching views with `#view` keeps the old stylesheets, so a CSS edit looks like it failed. Hard-reload, or change the query string. _Retire when: the dashboard serves assets with a content hash._
- **[2026-09-13] `text-overflow: ellipsis` needs a real child element** — a flex container's bare text is an anonymous flex item and cannot be ellipsized. Wrap the text in a span, as `uiBadge` does with `.badge-t`. _Retire when: badges stop being flex containers._
- **[2026-09-13] `oklab()` and `color()` values are on a 0–1 scale, not 0–255** — a contrast check that parses computed colours as 0–255 reports nonsense. Convert the colour through a canvas first. _Retire when: a contrast test exists in `evals/deterministic/`._
- **[2026-09-14] python-dotenv's `set_key` replaces a symlinked `.env` with a real file** — saving a setting from the dashboard inside a worktree detaches the linked `.env`. Check that `.env` is still a symlink before deleting a worktree. _Retire when: settings stop being written through `set_key`._
