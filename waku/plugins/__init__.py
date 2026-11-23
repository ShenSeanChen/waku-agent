"""Plugins: MCP servers, hooks and skills that arrive together (spec 021).

A plugin is a folder. Waku reads four parts of it and ignores the rest:

  .waku-plugin/plugin.json   name, version, description, mcpServers, hooks, skills
  hooks/<file>.json          hooks, in Claude Code's format
  skills/<name>/SKILL.md     skills, in the format Waku already loads

A plugin written for Claude Code alone (`.claude-plugin/plugin.json` and
`.mcp.json`) loads its servers and skills, and Waku skips its hooks: a Claude
Code hooks file names `claude_code` as its harness, and running it here would
record Waku's turns as Claude Code's.

`waku plugin install <name>@<marketplace>` copies one plugin into
<home>/plugins/<marketplace>/<name>/ after the person has read what it brings.
Installing runs nothing. Every network call a plugin causes goes to a server
the person saw and confirmed at install.

  manifest.py     one plugin folder, read into a Plugin
  store.py        what is installed and enabled, in <home>/plugins/installed.json
  marketplace.py  where plugins come from, and fetching one
  cli.py          `waku plugin ...` and the dashboard chat's `/plugin ...`
  hooks.py        running a plugin's hooks at the four events
"""
