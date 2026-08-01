"""Entrypoints — installed as the `milli` command (and `python -m milli`):

  milli                       chat in the terminal (default)
  milli dashboard             the browser cockpit → localhost:7777 (+ Telegram if configured)
  milli voice                 talk to it (needs the [voice] extra)
  milli telegram              phone → laptop (needs TELEGRAM_BOT_TOKEN)
  milli discord               Discord → laptop (needs DISCORD_BOT_TOKEN)
  milli whatsapp              WhatsApp → laptop (needs WHATSAPP_TOKEN, public URL)
  milli brief                 morning briefing (calendar + mail + memory) — as a LOOP
  milli gather                same job as a GRAPH: github, web, calendar and
                             memory fetched together, then one digest
  milli skill install <url>   install a community skill
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args:
        from milli.gateway.cli import main as cli_main

        cli_main()
    elif args[0] == "dashboard":
        from milli.ops.dashboard import main as dash_main

        dash_main()
    elif args[0] == "voice":
        from milli.gateway.voice import main as voice_main

        voice_main()
    elif args[0] == "telegram":
        from milli.gateway.telegram import main as tg_main

        tg_main()
    elif args[0] == "discord":
        from milli.gateway.discord import main as discord_main

        discord_main()
    elif args[0] == "whatsapp":
        from milli.gateway.whatsapp import main as wa_main

        wa_main()
    elif args[0] == "brief":
        from milli.ops.brief import main as brief_main

        brief_main()
    elif args[0] == "gather":
        from milli.ops.gather import main as gather_main

        gather_main()
    elif args[0] == "skill" and len(args) >= 3 and args[1] == "install":
        from milli.memory.procedural.installer import install

        install(args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
