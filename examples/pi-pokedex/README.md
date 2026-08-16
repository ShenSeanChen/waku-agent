# pi-pokedex

A tiny [pi](https://github.com/earendil-works/pi) package that bundles a **skill**
and an **extension** into one installable unit — the "earn your badge and ship it"
stage of the trainer's journey.

What's inside:

- **skill `pokedex`** — looks up any Pokemon's types, stats, and abilities via the
  public PokeAPI (no key). Loaded on demand; costs ~0 tokens until you ask.
- **extension `pokemon-battle`** — registers a deterministic `type_matchup` tool,
  and installs a "Team Rocket" guard that blocks bash commands from wiping runtime
  data (`.waku`, `rm -rf /`).

## Install

```bash
pi install ./pi-pokedex          # local path
pi install git:github.com/<you>/pi-pokedex   # or from git, for your team
pi list                          # confirm it's registered
```

Try it without installing (temporary, this run only):

```bash
pi -e ./pi-pokedex -p "What is super-effective against Charizard?"
```

## The point

Nothing here is new code versus the loose files in `.pi/extensions/` and
`.agents/skills/` — the only addition is `package.json`'s `pi` key, which declares
the resources so they travel as one versioned unit. **Extension = the app;
package = the App Store listing.**
