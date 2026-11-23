# Connection logo sources

The connection card logos are stored locally so the Dashboard never depends on
a third-party CDN at runtime.

- `telegram.svg`, `discord.svg`, `google_calendar.svg`, `apple_calendar.svg`,
  `apple_tools.svg`, `notion.svg`, `supabase.svg`, `otel.svg`, and
  `whatsapp.svg` use paths from
  [Simple Icons 16.21.0](https://github.com/simple-icons/simple-icons), licensed
  under CC0 1.0. Colors and rounded backgrounds are Waku presentation wrappers.
- `slack.svg` and `github.svg` use paths from Simple Icons 2.13.0, the same
  set at an older release and under the same CC0 1.0. The paths were copied
  from the `si` icons of `@react-icons/all-files` 4.1.0, which packages that
  release. Slack's is its mark in one colour, not the four-colour one.
  Simple Icons dropped Slack in a later release, which is why this file does
  not come from 16.21.0.
- `linear.svg` uses the path of Linear's own logomark: `logo-dark.svg` in
  <https://static.linear.app/design-assets/Linear-Brand-Assets.zip?v=3>, the
  archive that <https://linear.app/brand> links as "Download Linear logo",
  fetched on 2026-11-19. The path and its colour are Linear's, unchanged. The
  white tile around it is a Waku presentation wrapper. Linear's brand page
  sets the terms of use; the mark is not CC0 and not MIT.
- `tavily.svg` uses the `tavily-color` artwork from
  [Lobe Icons 1.90.0](https://github.com/lobehub/lobe-icons), licensed under MIT.
  Copyright (c) 2023 LobeHub.

- `typesafe.svg` is a neutral glyph drawn for this repository (a list with a
  check, for "Jev decides what to keep"), MIT like the rest of `waku/`. It is
  not TypeSafe's mark, which no icon set above carries.
- `treg.svg` is a neutral glyph drawn for this repository (a globe, for "live
  data from the web"), MIT like the rest of `waku/`. It is not treg's mark.

Brand names and marks remain the property of their respective owners.
