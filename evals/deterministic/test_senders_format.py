"""DETERMINISTIC EVAL: each sender formats for its channel (spec 024, decision 2).

The model writes Markdown. Telegram showed it raw: a reply read `**Calendar**`.
The Telegram sender now converts it to Telegram's HTML and sends
`parse_mode: HTML`, the Slack sender converts it to Slack's mrkdwn, and the
GitHub sender sends Markdown unchanged. A message Telegram refuses for its
markup goes out again as plain text, so a formatting error never loses a reply.

The senders post to a fake; nothing here opens a connection.
"""

from __future__ import annotations

import pytest

from waku.alwayson.outbox import Message
from waku.alwayson.senders import SendFailed, format, github, slack, telegram


def _message(body: str, channel: str = "telegram", target: dict | None = None) -> Message:
    return Message(1, "7", channel, target or {"chat_id": 42}, body, "7:0", "pending", 0,
                   "2026-11-17T22:52:00+00:00", "")


class FakePost:
    """Stands in for the HTTPS request. It answers from `answers`, then ok."""

    def __init__(self, *answers: dict) -> None:
        self.answers, self.payloads = list(answers), []

    def __call__(self, url, payload, headers):
        self.payloads.append(payload)
        return self.answers.pop(0) if self.answers else {"ok": True}


# ---- Telegram --------------------------------------------------------------

def test_telegram_bold_goes_out_as_html():
    """Acceptance 2."""
    post = FakePost()
    telegram.make("TOKEN", post=post)(_message("**Calendar**"))

    assert post.payloads == [{"chat_id": 42, "text": "<b>Calendar</b>", "parse_mode": "HTML"}]


@pytest.mark.parametrize("markdown, html", [
    ("**Calendar**: 2 events", "<b>Calendar</b>: 2 events"),
    ("__Calendar__", "<b>Calendar</b>"),
    ("an *important* word", "an <i>important</i> word"),
    ("an _important_ word", "an <i>important</i> word"),
    ("run `make eval` now", "run <code>make eval</code> now"),
    ("```python\nprint(1 < 2)\n```", "<pre>print(1 &lt; 2)\n</pre>"),
    ("see [the issue](https://github.com/acme/app/issues/12?a=1&b=2)",
     'see <a href="https://github.com/acme/app/issues/12?a=1&amp;b=2">the issue</a>'),
    ("## Today\nNothing.", "<b>Today</b>\nNothing."),
    ("**a *nested* word**", "<b>a <i>nested</i> word</b>"),
])
def test_telegram_html_converts_the_marks_telegram_knows(markdown, html):
    assert format.telegram_html(markdown) == html


@pytest.mark.parametrize("text", [
    "* one\n* two",                # a list, not italics
    "- one\n- two",
    "2 * 3 * 4 = 24",
    "the file my_first_run.py",
    "5 > 3",
])
def test_telegram_html_leaves_what_is_not_a_mark(text):
    assert format.telegram_html(text) == text.replace(">", "&gt;")


def test_telegram_html_escapes_what_telegram_would_read_as_a_tag():
    assert format.telegram_html("if a < b && b > c: <script>") == (
        "if a &lt; b &amp;&amp; b &gt; c: &lt;script&gt;")
    assert format.telegram_html("`<b>` is **bold**") == "<code>&lt;b&gt;</code> is <b>bold</b>"


def test_telegram_html_leaves_marks_inside_code_alone():
    assert format.telegram_html("`**not bold**`") == "<code>**not bold**</code>"


def test_telegram_refused_for_its_markup_is_sent_again_as_plain_text():
    """Acceptance 2: a formatting error never loses a reply."""
    post = FakePost({"ok": False, "error_code": 400,
                     "description": "Bad Request: can't parse entities: Unsupported start tag"})
    telegram.make("TOKEN", post=post)(_message("**Calendar** and `code` and [a link](https://x.io)"))

    assert len(post.payloads) == 2
    assert post.payloads[0]["parse_mode"] == "HTML"
    assert post.payloads[1] == {"chat_id": 42, "text": "Calendar and code and a link (https://x.io)"}


def test_telegram_refused_twice_fails_the_delivery():
    post = FakePost({"ok": False, "description": "Bad Request: can't parse entities"},
                    {"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(SendFailed, match="chat not found"):
        telegram.make("TOKEN", post=post)(_message("**Calendar**"))
    assert len(post.payloads) == 2


def test_telegram_refused_for_another_reason_is_not_sent_again():
    post = FakePost({"ok": False, "description": "Forbidden: bot was blocked by the user"})
    with pytest.raises(SendFailed, match="blocked"):
        telegram.make("TOKEN", post=post)(_message("**Calendar**"))
    assert len(post.payloads) == 1


def test_telegram_long_reply_goes_out_in_parts_that_each_parse():
    """A part never ends inside a mark: the text is cut at a line end, and
    each part is converted by itself."""
    line = "**Calendar** has an event.\n"
    post = FakePost()
    telegram.make("TOKEN", post=post)(_message(line * 400))

    assert len(post.payloads) > 1
    for payload in post.payloads:
        read = payload["text"].replace("<b>", "").replace("</b>", "")
        assert len(read) <= telegram.MAX_CHARS, "Telegram counts the text without its tags"
        assert payload["text"].count("<b>") == payload["text"].count("</b>") > 0
    sent = "".join(p["text"] + "\n" for p in post.payloads)
    assert sent.count("<b>Calendar</b>") == 400


def test_telegram_one_line_longer_than_the_limit_is_still_sent_whole():
    post = FakePost()
    telegram.make("TOKEN", post=post)(_message("x" * 9000))
    assert "".join(p["text"] for p in post.payloads) == "x" * 9000


# ---- Slack -----------------------------------------------------------------

@pytest.mark.parametrize("markdown, mrkdwn", [
    ("**Calendar**: 2 events", "*Calendar*: 2 events"),
    ("an *important* word", "an _important_ word"),
    ("run `make eval` now", "run `make eval` now"),
    ("see [the issue](https://github.com/acme/app/issues/12)",
     "see <https://github.com/acme/app/issues/12|the issue>"),
    ("## Today\nNothing.", "*Today*\nNothing."),
    ("a < b & c", "a &lt; b &amp; c"),
    ("* one\n* two", "* one\n* two"),
])
def test_slack_mrkdwn_converts_the_marks_slack_knows(markdown, mrkdwn):
    assert format.slack_mrkdwn(markdown) == mrkdwn


def test_slack_bold_goes_out_as_mrkdwn():
    post = FakePost()
    slack.make("xoxb-test", post=post)(
        _message("**Calendar**", "slack", {"channel": "C1", "thread_ts": "1700.0001"}))

    assert post.payloads == [{"channel": "C1", "text": "*Calendar*", "thread_ts": "1700.0001"}]


# ---- GitHub ----------------------------------------------------------------

def test_github_gets_the_markdown_unchanged():
    calls = []

    def gh(argv):
        calls.append(argv)
        return True, '{"comments": []}'

    github.make(run=gh)(_message("**Calendar** and `code`", "github",
                                 {"repo": "acme/app", "number": 12}))

    assert calls[1][6].startswith("--body=**Calendar** and `code`\n")


# ---- plain text ------------------------------------------------------------

def test_plain_removes_the_marks_and_keeps_the_words():
    assert format.plain("## Today\n**Calendar**: _one_ event, `id 7`, [link](https://x.io)") == (
        "Today\nCalendar: one event, id 7, link (https://x.io)")
