---
name: pokedex
description: Look up any Pokemon's types, base stats, and abilities from the public PokeAPI. Use whenever the user mentions a specific Pokemon by name or asks about its stats, types, or abilities.
---

# Pokedex

Professor Oak's Pokedex — the agent reads this file only when a Pokemon comes up
(progressive disclosure), then runs the script below. Nothing is loaded into
context until it is needed. This is the "a CLI + its README replaces MCP" pattern.

## Look up a Pokemon

```bash
./pokedex.sh <name>      # e.g. ./pokedex.sh pikachu
```

Names are lowercase, hyphenated for forms (e.g. `mr-mime`, `charizard`).
The script hits `https://pokeapi.co` (public, no API key) and prints one clean
line: types, base HP / Attack / Defense / Speed, and abilities.

## When to combine with the battle tool

If the user asks "what beats X?", first run `./pokedex.sh X` to get X's types,
then call the `type_matchup` tool (from the pokemon-battle extension) on each of
those types to reason about offense and defense.
