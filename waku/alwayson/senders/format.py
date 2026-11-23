"""The model writes Markdown, and each channel reads something else (spec 024,
decision 2).

  telegram_html   Telegram's HTML subset: <b>, <i>, <code>, <pre>, <a>
  slack_mrkdwn    Slack's mrkdwn: *bold*, _italic_, <url|text>
  plain           the words with the marks removed, for a message a channel
                  refused for its markup

GitHub reads Markdown, so its sender converts nothing.

Only four marks are converted: bold, italic, code and links, plus a heading,
which becomes a bold line. Anything else stays as the model wrote it. A list
bullet, `2 * 3` and `my_file_name` are not marks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_BLOCK = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*$", re.MULTILINE)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
# One mark each side, with a letter or digit on neither outer side, so a
# bullet, a product and a name with underscores are left alone.
_ITALIC = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])"
                     r"|(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])")
# A converted piece waits under this token until the end, so a later rule
# never reads a mark inside code or inside a link's address.
_HELD = re.compile("\x00(\\d+)\x00")


@dataclass(frozen=True)
class _Style:
    escape: Callable[[str], str]
    bold: Callable[[str], str]
    italic: Callable[[str], str]
    code: Callable[[str], str]
    block: Callable[[str], str]
    link: Callable[[str, str], str]


def _render(text: str, style: _Style) -> str:
    held: list[str] = []

    def hold(piece: str) -> str:
        held.append(piece)
        return f"\x00{len(held) - 1}\x00"

    text = (text or "").replace("\x00", "")
    text = _BLOCK.sub(lambda m: hold(style.block(style.escape(m[1]))), text)
    text = _CODE.sub(lambda m: hold(style.code(style.escape(m[1]))), text)
    text = _LINK.sub(lambda m: hold(style.link(m[1], m[2])), text)
    text = style.escape(text)
    # A heading is one bold line; bold marks inside it would nest.
    text = _HEADING.sub(lambda m: "**" + m[1].replace("**", "").replace("__", "") + "**", text)
    # Italic first: Slack's bold is one asterisk, which the italic rule reads.
    text = _ITALIC.sub(lambda m: hold(style.italic(m[1] or m[2])), text)
    text = _BOLD.sub(lambda m: style.bold(m[1] or m[2]), text)
    while _HELD.search(text):
        text = _HELD.sub(lambda m: held[int(m[1])], text)
    return text


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TELEGRAM = _Style(
    escape=_escape_html,
    bold=lambda words: f"<b>{words}</b>",
    italic=lambda words: f"<i>{words}</i>",
    code=lambda words: f"<code>{words}</code>",
    block=lambda words: f"<pre>{words}</pre>",
    link=lambda words, url: '<a href="{}">{}</a>'.format(
        _escape_html(url).replace('"', "&quot;"), _escape_html(words)),
)

# Slack reads &, < and > as control characters, and code keeps its backticks.
_SLACK = _Style(
    escape=_escape_html,
    bold=lambda words: f"*{words}*",
    italic=lambda words: f"_{words}_",
    code=lambda words: f"`{words}`",
    block=lambda words: f"```{words}```",
    link=lambda words, url: f"<{_escape_html(url)}|{_escape_html(words)}>",
)

_PLAIN = _Style(
    escape=lambda text: text,
    bold=lambda words: words,
    italic=lambda words: words,
    code=lambda words: words,
    block=lambda words: words,
    link=lambda words, url: f"{words} ({url})",
)


def telegram_html(text: str) -> str:
    return _render(text, _TELEGRAM)


def slack_mrkdwn(text: str) -> str:
    return _render(text, _SLACK)


def plain(text: str) -> str:
    return _render(text, _PLAIN)


def parts(text: str, limit: int) -> list[str]:
    """`text` in pieces of at most `limit` characters, cut at a line end where
    there is one, so a piece does not end inside a mark."""
    pieces = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut])
        text = text[cut:].lstrip("\n") if text[cut:cut + 1] == "\n" else text[cut:]
    if text:
        pieces.append(text)
    return pieces
