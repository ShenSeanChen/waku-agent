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
pi install ./lab/pi-agent/pokedex   # local path, from the repo root
pi install git:github.com/<you>/pi-pokedex   # or from git, for your team
pi list                          # confirm it's registered
```

Try it without installing (temporary, this run only):

```bash
pi -e ./lab/pi-agent/pokedex -p "What is super-effective against Charizard?"
```

## The point

The skill and the extension are ordinary files. The only addition is
`package.json`'s `pi` key, which declares them so they travel as one versioned
unit. **Extension = the app;
package = the App Store listing.**
