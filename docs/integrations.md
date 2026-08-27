# Connect it to your life

Everything here is opt-in and behind its own extra. None of it changes the
loop — a gateway moves text in and out, an integration is a tool the agent
may call. The four pillars work with none of this installed.

Moved out of the README, which had grown to 569 lines for a project whose
promise is that you can read it in an afternoon. Nothing was rewritten.

## Talk to it

```bash
uv pip install -e '.[voice]'
waku voice        # hands-free: always-listening for "waku waku"
```

**Hands-free by default.** `waku voice` listens for the wake word **"waku waku"** — a tiny
Whisper model scans the mic; when it hears the phrase, the big model takes over for your
command and speaks the reply. Change or disable it:

```bash
WAKU_WAKE_WORD="hey waku"  waku voice     # any phrase, no training
WAKU_WAKE_WORD=""          waku voice     # push-to-talk instead (Enter, speak, Enter)
```

The matcher is ~15 transparent lines with a deterministic eval; it accepts cross-script
variants (`"waku waku,わくわく"`). A trained openWakeWord model is the efficient v2 upgrade.

**A beautiful voice.** Out of the box it uses macOS `say` — and Waku auto-picks the nicest
voice you have, preferring a downloaded Premium/Enhanced one (System Settings ▸ Accessibility
▸ Spoken Content ▸ System Voice) over the robotic built-ins. For the real neural upgrade,
install [Kokoro](https://github.com/hexgrad/kokoro) — a fully local, offline British-butler
voice that's picked up automatically, no env var needed:

```bash
uv pip install '.[voice-neural]'          # neural Kokoro (bm_george); pulls torch (~2GB)
```

Override either engine with `WAKU_VOICE` (a `say` voice name, or a Kokoro voice like `bf_emma`).

## Phone to laptop

```bash
pip install -e '.[telegram]'
# message @BotFather, /newbot, put the token in .env, then:
make telegram
```

Text your bot from anywhere and your laptop runs the turn — long-polling, so no
public URL or webhook. Set `TELEGRAM_ALLOWED_USER` to lock it to just you.

## Brief me on my week (Apple Calendar + Mail)

```bash
WAKU_APPLE_TOOLS=1 make brief      # macOS; grant the permission prompts once
```

Waku reads your **real** Calendar.app (including events invited by email) and
recent Apple Mail, cross-references your memory, and writes a focus-first briefing
with clickable `message://` links. Cron it for a morning greeting:

```
30 7 * * *  cd ~/waku-agent && make brief
```

It runs through the normal harness, so it animates on the dashboard like any turn.

## Mirror created events to Google Calendar

The local SQLite database and `calendar.ics` stay authoritative. To also write
`create_event` results to Google Calendar, install the opt-in extra and configure
[Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc):

```bash
pip install -e '.[gcal]'
# Keep the downloaded client file OUTSIDE the repo — it is only an input to
# gcloud, which stores the resulting credentials in ~/.config/gcloud/.
gcloud auth application-default login \
  --client-id-file=~/.config/waku/gcal-client.json \
  --scopes=https://www.googleapis.com/auth/calendar.events
WAKU_GOOGLE_CALENDAR=1 waku
```

Nothing secret ever needs to live in the repo: the client file is read once by
`gcloud`, and the credentials it mints land in `~/.config/gcloud/`. (`.gitignore`
also blocks `credentials.json` and `*token*.json` as a second line of defence.)

The target defaults to the signed-in user's `primary` calendar; set
`WAKU_GOOGLE_CALENDAR_ID` for another calendar. `list_events` still reads the
local database. Google failures never roll back the local event, and attendee
notifications are suppressed (`sendUpdates=none`).

## Connect MCP servers

```bash
pip install -e '.[mcp]'
```

Create `.waku/mcp.json` and any Model Context Protocol server's tools appear to
the agent, namespaced `<server>_<tool>` (and in the dashboard's Tools ▸ MCP tab):

```json
{"servers": [{"name": "fs", "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}]}
```

**Node-free demo** — a tiny self-contained Python MCP server ships in the repo:

```bash
cp examples/mcp.demo.json .waku/mcp.json   # points at examples/mcp_demo_server.py
make dashboard                               # demo_word_count / demo_reverse_text appear in Tools
```

Same pattern scales to any server, yours or a vendor's — no changes to Waku's code.

### Remote servers (Streamable HTTP)

A server that is already running somewhere else is named by `url` instead of
`command`. This is the MCP spec's transport for remote servers; the older
HTTP+SSE transport is deprecated and is not supported.

```json
{"servers": [{"name": "waku_memory",
              "url": "https://your-host/mcp",
              "auth_env": "WAKU_MEMORY_API_KEY"}]}
```

`auth_env` names an **environment variable**; its value is sent as
`Authorization: Bearer <value>`. The credential never goes in `mcp.json` —
that file gets pasted into bug reports, and a bearer token in one is a leaked
credential. If the variable is not exported, Waku says so by name rather than
connecting anonymously and letting the server's 401 look like an outage.

Try it against the demo server, no remote host required:

```bash
python examples/mcp_demo_server.py --http --port 8931
# .waku/mcp.json → {"servers": [{"name": "demo", "url": "http://127.0.0.1:8931/mcp"}]}
```

Requires `mcp>=2.1` (`pip install -e '.[mcp]'`). The 1.x SDK spelled this
transport differently and the remote branch does not work on it.

