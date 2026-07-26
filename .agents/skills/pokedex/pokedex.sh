#!/usr/bin/env bash
# Pokedex lookup via the public PokeAPI (no API key). Prints one clean line.
set -euo pipefail

name="${1:?usage: pokedex.sh <pokemon-name>}"
url="https://pokeapi.co/api/v2/pokemon/$(echo "$name" | tr '[:upper:]' '[:lower:]')"

curl -fsS "$url" | python3 -c '
import json, sys
d = json.load(sys.stdin)
stats = {s["stat"]["name"]: s["base_stat"] for s in d["stats"]}
types = ", ".join(t["type"]["name"] for t in d["types"])
abilities = ", ".join(a["ability"]["name"] for a in d["abilities"])
name = d["name"].title()
hp, atk, dfn = stats["hp"], stats["attack"], stats["defense"]
spa, spd_def, spd = stats["special-attack"], stats["special-defense"], stats["speed"]
total = hp + atk + dfn + spa + spd_def + spd
print("%s  #%d" % (name, d["id"]))
print("  types:     %s" % types)
print("  base:      HP %d / Atk %d / Def %d / SpA %d / SpD %d / Spd %d  (total %d)"
      % (hp, atk, dfn, spa, spd_def, spd, total))
print("  abilities: %s" % abilities)
' || { echo "pokedex: could not find \"$name\" (try lowercase, e.g. pikachu)"; exit 1; }
