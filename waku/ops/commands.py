"""Slash commands — call a graph workflow by name.

    /gather      run the morning gather
    /graphs      list what is available

Two kinds of graph ship in Waku and conflating them made the UI say something
false. `triage` is a ROUTER: it runs itself on every message and you should
never think about it. `gather` is a PROCEDURE: a named shape you ask for. The
dashboard used to claim "you never pick a mode", which was true until the
second kind existed.

A graph is a pre-determined workflow. If the shape is known in advance, being
able to name it is the natural interface — so anything with a binder gets a
slash command, automatically.

DISCOVERY, and why it is shaped this way: a module in waku/graph/workflows/ is
the PURE graph — injected callables, no client, no filesystem, nothing bound to
this machine. It cannot be run on its own, by design, because that is what
makes it testable. What makes a workflow runnable is its BINDER in waku/ops/,
where real callables meet the pure shape.

So the rule is: `waku/graph/workflows/<name>.py` plus `waku/ops/<name>.py`
exposing `run_<name>` gives you `/<name>`. Drop in both halves and the command
appears; ship only the pure half and it stays a shape the dashboard can draw
but nobody can invoke.

A runner that declares a `message` parameter receives whatever followed the
command. `gather` takes none — it always fetches the same four things, which is
what makes it a fixed shape. `triage` needs one, because a router with nothing
to route has nothing to decide.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil


def discover() -> dict[str, str]:
    """{command name: "module:function"} for every runnable workflow.

    Import errors are swallowed per workflow: one broken optional workflow must
    not take out the whole command list (and with it the chat box).
    """
    import waku.graph.workflows as pkg

    found: dict[str, str] = {}
    for mod in pkgutil.iter_modules(pkg.__path__):
        name = mod.name
        if name.startswith("_"):
            continue
        binder = f"waku.ops.{name}"
        try:
            module = importlib.import_module(binder)
        except Exception:  # noqa: S112 — one broken optional workflow must not
            continue       # empty the command list, and with it the chat box
        if callable(getattr(module, f"run_{name}", None)):
            found[name] = f"{binder}:run_{name}"
    return found


def describe() -> str:
    """The /graphs reply. Reads the binder's docstring first line so a new
    workflow describes itself instead of needing a second registration."""
    lines = ["**Graph workflows you can run by name**", ""]
    for name, target in sorted(discover().items()):
        module_name, _, _ = target.partition(":")
        try:
            doc = (importlib.import_module(module_name).__doc__ or "").strip()
            summary = doc.splitlines()[0] if doc else ""
        except Exception:
            summary = ""
        lines.append(f"`/{name}` — {summary}" if summary else f"`/{name}`")
    lines += [
        "",
        ("`triage` is the ROUTER — it also runs itself on every message when "
         "graph workflows are on, so calling it by name just lets you watch one "
         "message choose a door. The others are procedures: they only run when "
         "you ask."),
    ]
    return "\n".join(lines)


def parse(message: str) -> tuple[str, str] | None:
    """('graphs', '') / ('gather', 'rest of line') / None if not a command.

    A leading slash only counts at the very start and with no space after it,
    so a message that merely mentions a path is still a message.
    """
    text = (message or "").strip()
    if not text.startswith("/") or text.startswith("/ "):
        return None
    head, _, rest = text[1:].partition(" ")
    return head.strip().lower(), rest.strip()


def run(name: str, emit, arg: str = "") -> dict | None:
    """Run a workflow by name, streaming its node events through `emit`.

    `arg` is whatever followed the command. It is passed only to runners that
    declare a `message` parameter — a gather takes no input (the whole point is
    that it always fetches the same four things), while a router needs
    something to route. Inspecting the signature keeps that a property of each
    binder rather than a second table someone has to remember to update.

    Returns the final state, or None if the name is unknown. Never raises: a
    bad command must answer in the chat, not kill the stream.
    """
    target = discover().get(name)
    if target is None:
        return None
    module_name, _, fn_name = target.partition(":")
    fn = getattr(importlib.import_module(module_name), fn_name)
    if "message" in inspect.signature(fn).parameters:
        return fn(observer=emit, message=arg)
    state = fn(observer=emit)
    if arg and isinstance(state, dict):
        # Silently swallowing input is worse than not accepting it: `/gather
        # what's up with the repo` looked like it had been understood, and the
        # digest that came back was the same one it always produces.
        state["ignored_argument"] = arg
    return state


def unknown_reply(name: str) -> str:
    known = sorted(discover())
    listed = ", ".join(f"`/{k}`" for k in known) or "(none installed)"
    return f"No workflow called `/{name}`.\n\nAvailable: {listed} · `/graphs` for details."
