"""The first-run question: keep your memory on Waku Memory, or on this machine?

Spec 020 A. A person who has not chosen is asked once, in the terminal (`waku`)
or on the dashboard's Overview page. Yes installs the Waku plugin, records
`plugin` and signs in (spec 021); when the plugin cannot be installed, it
records `agent` and connects Waku Memory directly, as before. "Keep local
only" records `off`. Both are saved as WAKU_MEMORY_MODE in the same
.env a pasted model key goes to, and neither is asked again. Nothing is sent
before the answer.

A person whose mcp.json, or an installed plugin, already names Waku Memory is
not asked: they chose it before, and Waku keeps behaving as it did for them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from waku.config import MEMORY_MODES, Settings, env_write_path
from waku.tools.waku_memory import connect, is_waku_memory

WHAT_LEAVES = (
    "Waku Memory keeps what Waku learns on waku.one, so your other agents "
    "(Claude Code, Codex, the Waku mobile app and the Waku Agent tab) know it too. "
    "Waku installs the Waku plugin, which sends each message you type and each reply "
    "Waku gives to Waku's servers, signed in as you, and on to the AI providers that "
    "turn them into memories, named at www.waku.one/privacy. Each research report and "
    "each note you save is sent too. Your files, the commands Waku runs and your "
    "model keys are not sent."
)

# The plugin the first-run question installs (spec 021).
WAKU_PLUGIN = "waku@waku-memory"
WAKU_SERVER = "waku_memory"


def already_connected(home: Path) -> bool:
    """Whether mcp.json or an installed plugin already names Waku Memory.
    Reads files, opens nothing."""
    from waku.plugins import store

    try:
        servers = json.loads((home / "mcp.json").read_text(encoding="utf-8")).get("servers", [])
    except (OSError, ValueError):
        servers = []
    return any(is_waku_memory(s) for s in [*servers, *store.servers(home)])


def pending(settings: Settings) -> bool:
    """True while this person has not been asked."""
    return settings.memory_mode == "" and not already_connected(settings.home)


def save(mode: str) -> None:
    """Record the choice where a pasted key goes, and in this process."""
    if mode not in MEMORY_MODES:
        raise ValueError(f"memory mode must be one of {MEMORY_MODES}, not {mode!r}")
    from dotenv import set_key

    set_key(str(env_write_path()), "WAKU_MEMORY_MODE", mode)
    os.environ["WAKU_MEMORY_MODE"] = mode


def apply(mode: str, home: Path) -> str:
    """Record the answer and act on it. Returns one message for the person.
    `agent` signs in, which opens a browser and waits for the click."""
    if mode == "off":
        save("off")
        return ("Waku Memory stays off. Your memory stays on this machine. "
                "To connect later: waku connect waku-memory.")
    if mode in ("agent", "plugin"):
        installed, message = install_plugin(home)
        if not installed:
            save("agent")
            return (f"The Waku plugin could not be installed ({message}). "
                    "Connecting Waku Memory directly instead. " + connect(home))
        save("plugin")
        return message
    raise ValueError("choose plugin, agent or off")


def follow_plugins(home: Path) -> str:
    """Make the saved mode match what the installed plugins do (spec 021
    review). A plugin whose Stop hook reaches Waku Memory means `plugin`:
    the server extracts each turn, so consolidation must stop sending. With
    no such plugin, `plugin` goes back to `agent` when mcp.json still names
    Waku Memory. `off` is the person's choice and never changes here.

    Only the home Waku runs from has a saved mode, so a command run on
    another folder changes nothing. Returns a line for the person, or ""."""
    from waku.config import _memory_mode, load_settings
    from waku.plugins import store
    from waku.tools.waku_memory import captured_by

    if Path(home).resolve() != load_settings().home.resolve():
        return ""
    mode = _memory_mode(os.getenv("WAKU_MEMORY_MODE", ""))
    if mode == "off":
        return ""
    if any(captured_by(p) for p in store.enabled(home)):
        if mode == "plugin":
            return ""
        save("plugin")
        return ("Waku Memory now extracts each turn from the plugin's Stop hook, so Waku "
                "keeps facts on this machine and sends none itself.")
    if mode != "plugin":
        return ""
    try:
        servers = json.loads((home / "mcp.json").read_text(encoding="utf-8")).get("servers", [])
    except (OSError, ValueError):
        servers = []
    if any(is_waku_memory(s) for s in servers):
        save("agent")
        return "No plugin sends turns to Waku Memory now, so Waku sends each fact it keeps again."
    return ("No plugin sends turns to Waku Memory now, and mcp.json does not name it, so nothing "
            "reaches Waku Memory. Install the plugin again, or run: waku connect waku-memory")


FIRST_RUN_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop"}


def first_run_check(plugin) -> str | None:
    """Why the first-run question will not install `plugin` without showing
    it, or None. The person said yes to one sentence about Waku Memory, so
    the plugin may bring only Waku Memory's server at its own address and
    only the hooks that sentence describes (spec 021 review). Anything else
    goes through `waku plugin install`, which shows it first."""
    from waku.tools.waku_memory import URL

    servers = {(s.get("name"), str(s.get("url", "")).rstrip("/")) for s in plugin.servers}
    if servers != {(WAKU_SERVER, URL)} or any(s.get("command") for s in plugin.servers):
        return "The plugin brings more than Waku Memory's own server."
    if not set(plugin.hooks) <= FIRST_RUN_EVENTS:
        return "The plugin has hooks the first-run question does not describe."
    return None


def install_plugin(home: Path) -> tuple[bool, str]:
    """Install the Waku plugin and sign in to its server. The person already
    said yes to the sentence above, so the install asks nothing more.
    Returns whether the plugin is installed, and one line for the person."""
    from waku.plugins import cli, manifest, store
    from waku.plugins.marketplace import MarketplaceError
    from waku.tools.mcp_cli import _auth_file, _identity, sign_in
    from waku.tools.waku_memory import captured_by

    try:
        # the person's yes covers only a plugin first_run_check accepts
        reply = cli.install(home, WAKU_PLUGIN, lambda shown: True, check=first_run_check)
    except (MarketplaceError, OSError, store.UnsafePath) as exc:
        return False, str(exc)
    if "Installed" not in reply:
        return False, reply.splitlines()[-1].removesuffix(" Nothing was installed.")
    # Spec 021 review: the marketplace's plugin may not reach Waku Memory
    # from Waku Agent yet (one with no .waku-plugin manifest loads with its
    # hooks skipped). Recording `plugin` then would send nothing at all, so
    # the plugin is removed again and the caller connects the old way.
    installed = manifest.load(store.inside(home, store.read(home)[WAKU_PLUGIN]["path"]))
    if installed is None or not captured_by(installed):
        cli.remove(home, WAKU_PLUGIN)
        return False, ("the Waku plugin in the marketplace does not send Waku Agent's turns "
                       "to Waku Memory yet")
    token = _auth_file(home, WAKU_SERVER)
    if not token.exists():
        ok, line = sign_in(home, WAKU_SERVER)
        if not ok:
            return True, ("Installed the Waku plugin. The sign-in did not finish: "
                          f"{line.strip()} Run: waku mcp login {WAKU_SERVER}")
    return True, (f"Installed the Waku plugin and connected to Waku Memory as {_identity(token)}. "
                  "Restart Waku to load it.")


def ask(settings: Settings, input_fn=input, out=print) -> str | None:
    """The terminal question. Returns the mode recorded, or None when the
    person had nothing to answer. Enter means yes."""
    if not pending(settings):
        return None
    out(WHAT_LEAVES)
    answer = input_fn("Sign in to Waku Memory now? [Y/n] ").strip().lower()
    mode = "off" if answer in ("n", "no") else "plugin"
    out(apply(mode, settings.home))
    return mode


def maybe_ask(settings: Settings) -> str | None:
    """ask(), only where a person is there to answer: a terminal on both ends.
    A gateway, a pipe, a test and a hosted container all skip it, and
    WAKU_MEMORY_PROMPT=0 skips it on purpose."""
    if os.getenv("WAKU_MEMORY_PROMPT", "") == "0":
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    return ask(settings)


def card(settings: Settings) -> dict:
    """What the dashboard's Overview needs to show the question."""
    return {"pending": pending(settings), "what": WHAT_LEAVES}
