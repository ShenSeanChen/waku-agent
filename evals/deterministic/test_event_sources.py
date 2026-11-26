"""DETERMINISTIC EVAL: each source only inserts a row (spec 023, decision 1).

A handler under `waku serve` checks who sent the message, writes one row into
`events` and returns. It never calls waku.respond(). Telegram, Slack and GitHub
are driven here with fake updates, envelopes and `gh` output: no network.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from waku.alwayson.events import Inbox
from waku.db import connect


@pytest.fixture
def inbox(tmp_path):
    return Inbox(tmp_path)


def _rows(inbox):
    import json

    conn = connect(inbox.home)
    try:
        return [(r["source"], r["external_id"], r["session_id"], json.loads(r["payload"]),
                 r["status"]) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    finally:
        conn.close()


# ---- Telegram --------------------------------------------------------------

def _telegram_update(update_id=900, chat_id=42, user_id=42, text="hello", sent=None):
    async def reply_text(message):
        (sent if sent is not None else []).append(message)

    return SimpleNamespace(
        update_id=update_id,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, full_name="Sean Chen"),
        message=SimpleNamespace(text=text, reply_text=reply_text),
    )


def _fake_telegram(monkeypatch):
    """The parts of python-telegram-bot that _build_app touches. Returns the
    list the message callback lands in."""
    callbacks = []

    class Filter:
        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    class MessageHandler:
        def __init__(self, pattern, callback):
            callbacks.append(callback)

    app = SimpleNamespace(add_handler=lambda handler: None)
    builder = SimpleNamespace(token=lambda token: builder, build=lambda: app)
    monkeypatch.setitem(sys.modules, "telegram", SimpleNamespace(Update=object))
    monkeypatch.setitem(sys.modules, "telegram.ext", SimpleNamespace(
        Application=SimpleNamespace(builder=lambda: builder),
        ContextTypes=SimpleNamespace(DEFAULT_TYPE=object),
        MessageHandler=MessageHandler,
        filters=SimpleNamespace(TEXT=Filter(), COMMAND=Filter()),
    ))
    return callbacks


def _telegram_handler(monkeypatch, inbox):
    """The serve-mode handler, built the way sources.telegram.start builds it."""
    from waku.alwayson.sources import telegram as source
    from waku.gateway import telegram as gateway

    def no_runner():
        raise AssertionError("waku serve must not build a gateway runner")

    monkeypatch.setattr(gateway, "_new_runner", no_runner)
    callbacks = _fake_telegram(monkeypatch)
    source.build_app("fake-token", inbox)
    return callbacks[0]


def test_telegram_message_becomes_one_event_in_its_chats_session(monkeypatch, inbox):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)
    handle = _telegram_handler(monkeypatch, inbox)
    sent = []

    asyncio.run(handle(_telegram_update(update_id=900, chat_id=42, text="hello", sent=sent),
                       SimpleNamespace()))

    assert _rows(inbox) == [(
        "telegram", "900", "telegram:42",
        {"text": "hello", "sender": {"id": "42", "name": "Sean Chen"},
         "reply_to": {"chat_id": 42}},
        "pending",
    )]
    assert sent == [], "the handler stores the message and answers nothing itself"


def test_telegram_chats_do_not_share_a_session(monkeypatch, inbox):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)
    handle = _telegram_handler(monkeypatch, inbox)

    asyncio.run(handle(_telegram_update(update_id=1, chat_id=42), SimpleNamespace()))
    asyncio.run(handle(_telegram_update(update_id=2, chat_id=-10077), SimpleNamespace()))

    assert [row[2] for row in _rows(inbox)] == ["telegram:42", "telegram:-10077"]


def test_telegram_redelivered_update_is_one_event(monkeypatch, inbox):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER", raising=False)
    handle = _telegram_handler(monkeypatch, inbox)

    asyncio.run(handle(_telegram_update(update_id=900), SimpleNamespace()))
    asyncio.run(handle(_telegram_update(update_id=900), SimpleNamespace()))

    assert len(_rows(inbox)) == 1


def test_telegram_allowlist_still_refuses_a_stranger(monkeypatch, inbox):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER", "42, 43")
    handle = _telegram_handler(monkeypatch, inbox)
    sent = []

    asyncio.run(handle(_telegram_update(update_id=1, user_id=666, sent=sent), SimpleNamespace()))
    asyncio.run(handle(_telegram_update(update_id=2, user_id=43, sent=sent), SimpleNamespace()))

    assert sent == ["This Waku serves someone else. Run your own!"]
    assert [row[1] for row in _rows(inbox)] == ["2"], "only the allowlisted sender is stored"


def test_telegram_source_is_off_without_a_token(monkeypatch, inbox):
    from waku.alwayson.sources import telegram as source

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert source.start(inbox) is None


def test_telegram_source_names_the_extra_when_it_is_missing(monkeypatch, inbox):
    from waku.alwayson.sources import MissingExtra
    from waku.alwayson.sources import telegram as source

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setitem(sys.modules, "telegram", None)   # import telegram -> ImportError
    with pytest.raises(MissingExtra) as missing:
        source.start(inbox)
    assert "TELEGRAM_BOT_TOKEN" in str(missing.value)
    assert "pip install 'waku-agent[telegram]'" in str(missing.value)


# ---- Slack -----------------------------------------------------------------

def _slack_envelope(event_id="Ev900", team="T1", channel="C1", ts="1700000000.000100",
                    thread_ts=None, text="<@UBOT> is issue 12 still open?", **event):
    body = {"type": "app_mention", "user": "UANA", "text": text, "channel": channel, "ts": ts}
    if thread_ts:
        body["thread_ts"] = thread_ts
    body.update(event)
    return {"team_id": team, "event_id": event_id, "type": "event_callback", "event": body}


def _slack_record(inbox, envelope, team_id="T1", channels=("C1",)):
    from waku.alwayson.sources import slack

    return slack.record(inbox, envelope, team_id=team_id, channels=set(channels))


def test_slack_mention_becomes_one_event_in_its_threads_session(inbox):
    assert _slack_record(inbox, _slack_envelope(thread_ts="1699999999.000001")) is True

    assert _rows(inbox) == [(
        "slack", "Ev900", "slack:C1:1699999999.000001",
        {"text": "<@UBOT> is issue 12 still open?", "sender": {"id": "UANA"},
         "reply_to": {"channel": "C1", "thread_ts": "1699999999.000001"}},
        "pending",
    )]


def test_slack_mention_outside_a_thread_starts_one(inbox):
    """A reply goes to the thread, so a top-level mention is its own thread."""
    _slack_record(inbox, _slack_envelope(ts="1700000000.000100"))

    _, _, session_id, payload, _ = _rows(inbox)[0]
    assert session_id == "slack:C1:1700000000.000100"
    assert payload["reply_to"] == {"channel": "C1", "thread_ts": "1700000000.000100"}


def test_slack_redelivered_event_is_one_event(inbox):
    assert _slack_record(inbox, _slack_envelope(event_id="Ev900")) is True
    assert _slack_record(inbox, _slack_envelope(event_id="Ev900")) is False
    assert len(_rows(inbox)) == 1


@pytest.mark.parametrize("envelope, why", [
    (_slack_envelope(type="message"), "only app_mention events are taken"),
    (_slack_envelope(channel="C2"), "the channel is not in SLACK_ALLOWED_CHANNELS"),
    (_slack_envelope(team="T2"), "the event is from another workspace"),
    (_slack_envelope(user_team="T2"), "the sender belongs to another workspace"),
    (_slack_envelope(bot_id="B1"), "a bot wrote it"),
    (_slack_envelope(event_id=""), "it has no event id to deduplicate on"),
    ({"team_id": "T1", "event_id": "Ev1"}, "it has no event"),
])
def test_slack_ignores_what_it_was_not_asked_to_take(inbox, envelope, why):
    assert _slack_record(inbox, envelope) is False, why
    assert _rows(inbox) == []


def test_slack_with_no_allowed_channel_takes_nothing(inbox):
    """The allowlist is a closed set: an empty one allows no channel."""
    assert _slack_record(inbox, _slack_envelope(), channels=()) is False
    assert _rows(inbox) == []


def test_slack_allowed_channels_come_from_the_environment(monkeypatch):
    from waku.alwayson.sources import slack

    monkeypatch.setenv("SLACK_ALLOWED_CHANNELS", " C1, C2 ,,")
    assert slack.allowed_channels() == {"C1", "C2"}
    monkeypatch.delenv("SLACK_ALLOWED_CHANNELS")
    assert slack.allowed_channels() == set()


def _fake_slack_sdk(monkeypatch, order):
    """The parts of slack_sdk that sources.slack.start touches."""
    clients = []

    class WebClient:
        def __init__(self, token):
            self.token = token

        def auth_test(self):
            return {"team_id": "T1", "user_id": "UBOT"}

    class SocketModeClient:
        def __init__(self, app_token, web_client):
            self.app_token, self.web_client = app_token, web_client
            self.socket_mode_request_listeners = []
            self.connected = self.closed = False
            clients.append(self)

        def connect(self):
            self.connected = True

        def close(self):
            self.closed = True

        def send_socket_mode_response(self, response):
            order.append(("ack", response.envelope_id))

    class SocketModeResponse:
        def __init__(self, envelope_id):
            self.envelope_id = envelope_id

    monkeypatch.setitem(sys.modules, "slack_sdk", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode",
                        SimpleNamespace(SocketModeClient=SocketModeClient))
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.response",
                        SimpleNamespace(SocketModeResponse=SocketModeResponse))
    monkeypatch.setitem(sys.modules, "slack_sdk.web", SimpleNamespace(WebClient=WebClient))
    return clients


def test_slack_listener_stores_the_mention_before_it_acknowledges(monkeypatch, inbox):
    """Slack resends an event that was not acknowledged. Storing first means a
    crash between the two costs a redelivery, never a message."""
    from waku.alwayson.sources import slack

    order = []
    clients = _fake_slack_sdk(monkeypatch, order)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("SLACK_ALLOWED_CHANNELS", "C1")
    real_add = inbox.add
    monkeypatch.setattr(inbox, "add", lambda **row: order.append(("stored", row["external_id"]))
                        or real_add(**row))

    handle = slack.start(inbox)
    client = clients[0]
    assert client.connected
    listener = client.socket_mode_request_listeners[0]
    listener(client, SimpleNamespace(type="events_api", envelope_id="env-1",
                                     payload=_slack_envelope(event_id="Ev900")))
    listener(client, SimpleNamespace(type="interactive", envelope_id="env-2", payload={}))

    assert order == [("stored", "Ev900"), ("ack", "env-1"), ("ack", "env-2")]
    assert [row[1] for row in _rows(inbox)] == ["Ev900"]
    handle.stop()
    assert client.closed


def test_slack_source_is_off_without_both_tokens(monkeypatch, inbox):
    from waku.alwayson.sources import slack

    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    assert slack.start(inbox) is None


def test_slack_source_names_the_extra_when_it_is_missing(monkeypatch, inbox):
    from waku.alwayson.sources import MissingExtra, slack

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setitem(sys.modules, "slack_sdk", None)   # import slack_sdk -> ImportError
    with pytest.raises(MissingExtra) as missing:
        slack.start(inbox)
    assert "SLACK_APP_TOKEN" in str(missing.value)
    assert "pip install 'waku-agent[slack]'" in str(missing.value)


def test_slack_sdk_is_not_needed_to_import_waku(tmp_path):
    """The import guard: slack_sdk is behind the extra [slack], so `waku`,
    `waku serve` and the Slack source itself import without it."""
    import os
    import subprocess

    code = ("import sys; sys.modules['slack_sdk'] = None\n"
            "import waku, waku.__main__, waku.alwayson.serve, waku.alwayson.sources.slack\n"
            "from waku.config import Settings\n"
            "sources, routes = waku.alwayson.serve.configured(Settings())\n"
            "assert 'slack' in sources\n"
            "print('imported')")
    env = {"PATH": "", "HOME": str(tmp_path), "WAKU_HOME": str(tmp_path),
           "PYTHONPATH": str(__import__("pathlib").Path(__file__).parents[2])}
    if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=tmp_path, check=False, env=env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "imported"


# ---- GitHub ----------------------------------------------------------------

SECRET = "webhook-secret-for-tests"


def _sign(body: bytes, secret: str = SECRET) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _issue(number=12, title="Login page 500s", body="Steps to reproduce: open /login.",
           labels=("waku",), author="ana"):
    return {"number": number, "title": title, "body": body, "user": {"login": author},
            "labels": [{"name": name} for name in labels],
            "html_url": f"https://github.com/acme/app/issues/{number}"}


def _labeled(label="waku", **issue):
    return {"action": "labeled", "label": {"name": label}, "issue": _issue(**issue),
            "repository": {"full_name": "acme/app"}, "sender": {"login": "sean"}}


def _comment(body="@waku-bot is this still open?", author="ana", association="MEMBER"):
    return {"action": "created", "issue": _issue(labels=()),
            "comment": {"id": 5001, "body": body, "user": {"login": author},
                        "author_association": association,
                        "html_url": "https://github.com/acme/app/issues/12#issuecomment-5001"},
            "repository": {"full_name": "acme/app"}, "sender": {"login": author}}


def _post(inbox, data, *, event="issues", delivery="d-1", secret=SECRET, signature=None,
          login="waku-bot"):
    import json

    from waku.alwayson.sources import github

    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
               "X-Hub-Signature-256": _sign(body) if signature is None else signature}
    status, _ = github.webhook_route(inbox, secret, login)(headers, body)
    return status


def test_github_webhook_with_a_bad_signature_is_403_and_inserts_nothing(inbox):
    assert _post(inbox, _labeled(), signature="sha256=" + "0" * 64) == 403
    assert _post(inbox, _labeled(), signature="") == 403
    assert _post(inbox, _labeled(), signature=_sign(b"another body")) == 403
    assert _rows(inbox) == []


def test_github_webhook_without_a_configured_secret_accepts_nothing(inbox):
    """An empty secret must not verify a body signed with an empty key."""
    import json

    body = json.dumps(_labeled()).encode()
    assert _post(inbox, body, secret="", signature=_sign(body, "")) == 403
    assert _rows(inbox) == []


def test_github_issue_given_the_label_becomes_one_event(inbox):
    assert _post(inbox, _labeled(), delivery="d-900") == 200

    assert _rows(inbox) == [(
        "github", "d-900", "github:acme/app#12",
        {"text": 'GitHub issue acme/app#12 "Login page 500s" has the label waku. '
                 "ana opened it.\n\nSteps to reproduce: open /login.",
         "sender": {"login": "sean"},
         "reply_to": {"repo": "acme/app", "number": 12},
         "url": "https://github.com/acme/app/issues/12",
         "unprompted": "New issue acme/app#12: 'Login page 500s'"},
        "pending",
    )]


def test_github_issue_opened_with_the_label_becomes_one_event(inbox):
    opened = {"action": "opened", "issue": _issue(), "repository": {"full_name": "acme/app"},
              "sender": {"login": "ana"}}
    assert _post(inbox, opened) == 200
    assert [row[2] for row in _rows(inbox)] == ["github:acme/app#12"]


@pytest.mark.parametrize("event, data, why", [
    ("issues", _labeled(label="bug"), "another label was added"),
    ("issues", {"action": "opened", "issue": _issue(labels=("bug",)),
                "repository": {"full_name": "acme/app"}}, "it was opened without the label"),
    ("issues", {**_labeled(), "action": "closed"}, "closing is not asking"),
    ("issue_comment", _comment(body="no mention here"), "nobody mentioned the agent"),
    ("issue_comment", _comment(body="ask @waku-bot-two"), "another login was mentioned"),
    ("issue_comment", _comment(body="mail me at x@waku-bot"), "an address is not a mention"),
    ("issue_comment", _comment(association="NONE"), "the author has no role in the repository"),
    ("issue_comment", _comment(author="waku-bot"), "the agent wrote the comment itself"),
    ("issue_comment", {**_comment(), "action": "deleted"}, "the comment was deleted"),
    ("ping", {"zen": "Keep it logically awesome."}, "a ping carries nothing"),
    ("push", {"ref": "refs/heads/main"}, "pushes are not taken"),
])
def test_github_webhook_ignores_what_it_was_not_asked_to_take(inbox, event, data, why):
    assert _post(inbox, data, event=event) == 200, "a signed delivery is always acknowledged"
    assert _rows(inbox) == [], why


def test_github_comment_that_mentions_the_agent_becomes_one_event(inbox):
    assert _post(inbox, _comment(body="@Waku-Bot is this still open?"),
                 event="issue_comment", delivery="d-901") == 200

    source, external_id, session_id, payload, _ = _rows(inbox)[0]
    assert (source, external_id, session_id) == ("github", "d-901", "github:acme/app#12")
    assert payload["text"] == ('ana mentioned you in a comment on GitHub issue acme/app#12 '
                               '"Login page 500s":\n\n@Waku-Bot is this still open?')
    assert payload["sender"] == {"login": "ana"}
    assert payload["reply_to"] == {"repo": "acme/app", "number": 12}


def test_github_mentions_are_off_without_a_login(inbox):
    assert _post(inbox, _comment(), event="issue_comment", login="") == 200
    assert _rows(inbox) == []


def test_github_redelivered_webhook_is_one_event(inbox):
    assert _post(inbox, _labeled(), delivery="d-900") == 200
    assert _post(inbox, _labeled(), delivery="d-900") == 200
    assert len(_rows(inbox)) == 1


def test_github_webhook_refuses_a_signed_body_it_cannot_read(inbox):
    assert _post(inbox, b"not json") == 400
    import json

    from waku.alwayson.sources import github

    body = json.dumps(_labeled()).encode()
    status, _ = github.webhook_route(inbox, SECRET, "waku-bot")(
        {"X-GitHub-Event": "issues", "X-Hub-Signature-256": _sign(body)}, body)
    assert status == 400, "no X-GitHub-Delivery means nothing to deduplicate on"
    assert _rows(inbox) == []


class _FakeGh:
    """Stands in for the `gh` CLI: records every argv and answers from a table."""

    def __init__(self, issues=(), comments=(), ok=True, prs=()):
        self.calls, self.issues, self.comments, self.ok = [], list(issues), list(comments), ok
        self.prs = list(prs)

    def __call__(self, argv):
        import json

        self.calls.append(argv)
        if not self.ok:
            return False, "gh is not authenticated. Run `gh auth login`."
        if argv[1] == "pr":
            return True, json.dumps(self.prs)
        return True, json.dumps(self.issues if argv[1] == "issue" else self.comments)


def _poller(inbox, gh, **kwargs):
    from waku.alwayson.sources import github

    kwargs.setdefault("log", lambda line: None)
    return github.Poller(inbox, ["acme/app"], kwargs.pop("login", ""), run=gh, **kwargs)


_LISTED = {"number": 12, "title": "Login page 500s", "body": "Steps to reproduce: open /login.",
           "author": {"login": "ana"}, "url": "https://github.com/acme/app/issues/12"}


def test_github_poller_turns_a_labelled_issue_into_one_event(inbox):
    gh = _FakeGh(issues=[_LISTED])
    poller = _poller(inbox, gh)

    assert poller.poll_once() == 1
    assert poller.poll_once() == 0, "the same issue on the next poll is a no-op"

    assert gh.calls[0] == ["gh", "issue", "list", "--repo", "acme/app", "--label", "waku",
                           "--state", "open", "--limit", "50",
                           "--json", "number,title,body,author,url"]
    assert _rows(inbox) == [(
        "github", "label:acme/app#12", "github:acme/app#12",
        {"text": 'GitHub issue acme/app#12 "Login page 500s" has the label waku. '
                 "ana opened it.\n\nSteps to reproduce: open /login.",
         "sender": {"login": "ana"},
         "reply_to": {"repo": "acme/app", "number": 12},
         "url": "https://github.com/acme/app/issues/12",
         "unprompted": "New issue acme/app#12: 'Login page 500s'"},
        "pending",
    )]


def test_github_poller_reads_mentions_since_the_last_poll(inbox):
    comment = {"id": 5001, "body": "@waku-bot is this still open?", "user": {"login": "ana"},
               "author_association": "COLLABORATOR",
               "issue_url": "https://api.github.com/repos/acme/app/issues/12",
               "html_url": "https://github.com/acme/app/issues/12#issuecomment-5001"}
    stranger = {**comment, "id": 5002, "author_association": "NONE"}
    gh = _FakeGh(comments=[comment, stranger])
    times = iter(["2026-11-16T09:00:00Z", "2026-11-16T09:01:00Z", "2026-11-16T09:02:00Z"])
    poller = _poller(inbox, gh, login="waku-bot", now=lambda: next(times))

    assert poller.poll_once() == 1
    poller.poll_once()

    api_calls = [argv for argv in gh.calls if argv[1] == "api"]
    assert api_calls[0] == ["gh", "api", "--method", "GET", "repos/acme/app/issues/comments",
                            "-f", "since=2026-11-16T09:00:00Z", "-f", "per_page=100"]
    assert api_calls[1][6] == "since=2026-11-16T09:01:00Z", "the next poll starts where this one did"
    assert [(row[1], row[2], row[3]["text"]) for row in _rows(inbox)] == [(
        "comment:5001", "github:acme/app#12",
        ("ana mentioned you in a comment on GitHub issue acme/app#12:\n\n"
         "@waku-bot is this still open?"))]


def test_github_poller_without_a_login_never_reads_comments(inbox):
    gh = _FakeGh(issues=[_LISTED])
    _poller(inbox, gh, login="").poll_once()
    assert [argv[1] for argv in gh.calls] == ["issue", "pr"]


def test_github_poller_survives_a_failing_gh(inbox):
    logged = []
    gh = _FakeGh(ok=False)
    poller = _poller(inbox, gh, log=logged.append)

    assert poller.poll_once() == 0
    assert poller.poll_once() == 0
    assert _rows(inbox) == []
    assert len(logged) == 1 and "gh auth login" in logged[0], "the same failure is said once"


def test_github_poller_waits_60_seconds_between_polls(inbox):
    gh = _FakeGh()
    waits = []
    poller = _poller(inbox, gh)

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            poller.stop_event.set()

    poller.run_forever(wait=wait)
    assert waits == [60, 60]
    assert len(gh.calls) == 4, "two polls, each lists issues and pull requests"


# ---- spec 035: every new pull request, as a quiet row ------------------------

_PR_URL = "https://github.com/acme/app/pull/428"
_PR_TEXT = 'Pull request acme/app#428 "check full fact table for duplicates" by contributor, +140/-15.'
_PR_PAYLOAD = {
    "text": _PR_TEXT, "quiet": True, "sender": {"login": "contributor"}, "url": _PR_URL,
    "pr": {"repo": "acme/app", "number": 428, "title": "check full fact table for duplicates",
           "author": "contributor", "url": _PR_URL, "additions": 140, "deletions": 15,
           "body": "Checks every store for duplicate facts."}}
_PR_LISTED = {"number": 428, "title": "check full fact table for duplicates",
              "body": "Checks every store for duplicate facts.", "author": {"login": "contributor"},
              "url": _PR_URL, "isDraft": False, "additions": 140, "deletions": 15}


def _pull_request(action="opened", number=428, draft=False):
    return {"action": action, "number": number,
            "pull_request": {"number": number, "title": "check full fact table for duplicates",
                             "body": "Checks every store for duplicate facts.",
                             "user": {"login": "contributor"},
                             "html_url": f"https://github.com/acme/app/pull/{number}",
                             "draft": draft, "additions": 140, "deletions": 15},
            "repository": {"full_name": "acme/app"}, "sender": {"login": "contributor"}}


def test_github_source_sees_new_prs(inbox):
    """A contributor opens PR #428. Anyone may open a pull request, so no
    trust check applies: the row is quiet, stored `done`, and only the PR
    desk's morning run reads it. A draft waits until it is marked ready."""
    for action, number, draft, delivery in (
            ("opened", 428, False, "d-1"),
            ("ready_for_review", 429, False, "d-2"),
            ("opened", 430, True, "d-3"),            # a draft is not recorded
            ("synchronize", 431, False, "d-4"),      # a push is not a new pull request
            ("opened", 428, False, "d-1"),           # GitHub redelivers d-1
            ("ready_for_review", 428, False, "d-5")):  # the same PR again
        assert _post(inbox, _pull_request(action, number, draft), event="pull_request",
                     delivery=delivery) == 200

    rows = _rows(inbox)
    assert [(row[1], row[2], row[4]) for row in rows] == [
        ("pr:acme/app#428", "github:acme/app#428", "done"),
        ("pr:acme/app#429", "github:acme/app#429", "done")]
    assert rows[0][0] == "github" and rows[0][3] == _PR_PAYLOAD

    # The poller writes the same row, so a laptop and a server agree.
    polled = Inbox(inbox.home / "polled")
    polled.home.mkdir()
    gh = _FakeGh(prs=[_PR_LISTED, {**_PR_LISTED, "number": 430, "isDraft": True}])
    poller = _poller(polled, gh)
    assert poller.poll_once() == 1
    assert poller.poll_once() == 0, "the same open pull request on the next poll is a no-op"
    assert ["gh", "pr", "list", "--repo", "acme/app", "--state", "open", "--limit", "50",
            "--json", "number,title,body,author,url,isDraft,additions,deletions"] in gh.calls
    assert [(row[1], row[3], row[4]) for row in _rows(polled)] == [
        ("pr:acme/app#428", _PR_PAYLOAD, "done")]


def test_github_repos_are_validated_before_any_process_exists(monkeypatch, capsys):
    from waku.alwayson.sources import github

    monkeypatch.setenv("WAKU_GITHUB_REPOS", "acme/app, --repo=evil/x ,acme/app; rm -rf ~, org/site")
    assert github.repos() == ["acme/app", "org/site"]
    assert "--repo=evil/x" in capsys.readouterr().out


def test_github_poller_is_the_default_and_the_webhook_replaces_it(monkeypatch, inbox):
    from waku.alwayson.sources import github

    started = []
    monkeypatch.setattr(github.Poller, "start", lambda self: started.append(self.repos) or self)
    for name in ("WAKU_GITHUB_REPOS", "GITHUB_WEBHOOK_SECRET", "WAKU_GITHUB_LOGIN"):
        monkeypatch.delenv(name, raising=False)
    assert github.start(inbox) is None, "nothing is configured"
    assert github.route(inbox) is None

    monkeypatch.setenv("WAKU_GITHUB_REPOS", "acme/app")
    assert github.start(inbox) is not None
    assert started == [["acme/app"]]
    assert github.route(inbox) is None, "no secret, so nothing listens"

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    assert github.start(inbox) is None, "the webhook delivers the same events"
    assert started == [["acme/app"]]
    assert github.route(inbox) is not None


def test_github_webhook_through_waku_serve(monkeypatch, tmp_path):
    """The route as `waku serve` mounts it: POST /webhooks/github on loopback."""
    import json
    import urllib.error
    import urllib.request

    from waku.alwayson import serve
    from waku.config import Settings

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    for name in ("WAKU_GITHUB_REPOS", "TELEGRAM_BOT_TOKEN", "SLACK_APP_TOKEN", "WAKU_SERVE_HOST"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(home=tmp_path)
    settings.ensure_home()
    sources, routes = serve.configured(settings)
    service = serve.Service(settings, sources=sources, routes=routes, port=0, batch_wait=0,
                            run=lambda session_id, batch: None, log=lambda line: None)
    service.start()
    try:
        host, port = service.address
        assert host == "127.0.0.1"
        body = json.dumps(_labeled()).encode()

        def post(signature):
            return urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/webhooks/github", data=body, method="POST",
                headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-900",
                         "X-Hub-Signature-256": signature}), timeout=5)

        with pytest.raises(urllib.error.HTTPError) as refused:
            post("sha256=" + "0" * 64)
        assert refused.value.code == 403
        assert _rows(Inbox(tmp_path)) == []

        assert post(_sign(body)).status == 200
    finally:
        service.stop()
    assert [(row[0], row[1], row[2]) for row in _rows(Inbox(tmp_path))] == [
        ("github", "d-900", "github:acme/app#12")]


# ---- spec 024, decision 5: a run that no person started ---------------------

def _unprompted(inbox, monkeypatch, reply, log=None):
    """A new issue with the label, read by the poller, then one worker pass
    with a run that answers `reply`. Returns (the gh calls, the outbox rows,
    what the run was given)."""
    import json

    from waku.alwayson.dispatcher import Dispatcher
    from waku.alwayson.senders import github as github_sender
    from waku.alwayson.worker import Worker, render

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    monkeypatch.delenv("WAKU_APPROVE_ASK", raising=False)
    gh, given, told = _FakeGh(issues=[_LISTED]), [], []
    assert _poller(inbox, gh).poll_once() == 1
    del gh.calls[:]

    def run(session_id, batch):
        given.append(render(batch))
        return reply

    conn = connect(inbox.home)
    try:
        worker = Worker(conn, run, wait=lambda seconds: None, log=log or (lambda line: None))
        while worker.step():
            pass
        Dispatcher(conn, {"github": github_sender.make(run=gh),
                          "telegram": lambda message: told.append(message.body)},
                   log=lambda line: None).step()
        rows = [(r["channel"], json.loads(r["target"]), r["body"])
                for r in conn.execute("SELECT * FROM outbox ORDER BY id")]
    finally:
        conn.close()
    assert told == [body for _, _, body in rows], "every row went to the owner on Telegram"
    return gh.calls, rows, given[0]


def test_unprompted_issue_that_needs_nothing_gets_no_comment_and_one_line_to_the_owner(
        inbox, monkeypatch):
    """Spec 024, acceptance 5. Issues 1 and 2 each got "Got it, noted that
    issue ... No action taken" and Sean was told nothing."""
    gh_calls, rows, _ = _unprompted(inbox, monkeypatch, "SILENT")

    assert gh_calls == [], "zero GitHub comments"
    assert rows == [("telegram", {"chat_id": 42},
                     "New issue acme/app#12: 'Login page 500s'. I did nothing.")]


def test_unprompted_issue_the_agent_wants_to_comment_on_asks_first(inbox, monkeypatch):
    """Acceptance 5: zero comments, one request, one line."""
    gh_calls, rows, _ = _unprompted(inbox, monkeypatch, "I can reproduce this on Safari 17.")

    assert gh_calls == [], "zero GitHub comments before a yes"
    assert len(rows) == 2
    assert 'Comment on acme/app#12: "I can reproduce this on Safari 17."' in rows[0][2]
    assert "Approve? Reply yes or no." in rows[0][2]
    assert rows[1][:2] == ("telegram", {"chat_id": 42})
    assert rows[1][2] == ("New issue acme/app#12: 'Login page 500s'. I want to comment, "
                          "see the request above.")


def test_unprompted_run_is_told_that_silence_is_the_right_answer(inbox, monkeypatch):
    _, _, given = _unprompted(inbox, monkeypatch, "SILENT")

    assert 'GitHub issue acme/app#12 "Login page 500s" has the label waku.' in given
    assert "Nobody asked you" in given and "SILENT" in given
    assert "the owner reads it first" in given


def test_unprompted_issue_with_replies_allowed_says_that_it_commented(inbox, monkeypatch):
    import json

    from waku.alwayson.worker import Worker

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.setenv("WAKU_APPROVE_ALLOW", "reply:github")
    assert _poller(inbox, _FakeGh(issues=[_LISTED])).poll_once() == 1
    conn = connect(inbox.home)
    try:
        Worker(conn, lambda session_id, batch: "Thanks, I will look.",
               wait=lambda seconds: None, log=lambda line: None).step()
        rows = [(r["channel"], json.loads(r["target"]), r["body"])
                for r in conn.execute("SELECT * FROM outbox ORDER BY id")]
    finally:
        conn.close()

    assert rows == [
        ("github", {"repo": "acme/app", "number": 12}, "Thanks, I will look."),
        ("telegram", {"chat_id": 42}, "New issue acme/app#12: 'Login page 500s'. I commented.")]


def test_unprompted_line_is_written_once_when_the_run_is_replayed(inbox, monkeypatch):
    from waku.alwayson import events
    from waku.alwayson.worker import Worker

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    assert _poller(inbox, _FakeGh(issues=[_LISTED])).poll_once() == 1
    conn = connect(inbox.home)
    try:
        worker = Worker(conn, lambda session_id, batch: "SILENT",
                        wait=lambda seconds: None, log=lambda line: None)
        worker.step()
        events.mark(conn, [1], "claimed")   # the process died before the mark
        worker.recover()
        worker.step()
        count = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_unprompted_mention_is_a_person_asking_and_writes_no_line(inbox, monkeypatch):
    """A comment that mentions the agent is a person asking. Its answer still
    waits for a yes, and the owner reads no "New issue" line."""
    from waku.alwayson.sources import github
    from waku.alwayson.worker import Worker

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")
    monkeypatch.delenv("WAKU_APPROVE_ALLOW", raising=False)
    found = github._mention_event("acme/app", 12, "Login page 500s", "@waku-bot open?",
                                  "ana", "")
    assert "unprompted" not in found["payload"]
    inbox.add(source="github", external_id="comment:1", **found)
    conn = connect(inbox.home)
    try:
        Worker(conn, lambda session_id, batch: "It is open.", wait=lambda seconds: None,
               log=lambda line: None).step()
        bodies = [r["body"] for r in conn.execute("SELECT body FROM outbox ORDER BY id")]
    finally:
        conn.close()
    assert len(bodies) == 1 and "Approve?" in bodies[0]


# ---- spec 025, decision 5: the Slack app is created from a manifest ---------

def _yaml(text: str):
    """The small part of YAML the manifest uses: nested mappings by two-space
    indentation, lists of scalars, and scalars. PyYAML is not a dependency,
    and Slack reads the same file."""
    lines = [(len(raw) - len(raw.lstrip(" ")), raw.strip())
             for raw in text.splitlines() if raw.strip() and not raw.strip().startswith("#")]

    def scalar(value: str):
        value = value.strip()
        if value in ("true", "false"):
            return value == "true"
        return value.strip("\"'")

    def block(at: int, indent: int):
        if lines[at][1].startswith("- "):
            items = []
            while at < len(lines) and lines[at][0] == indent and lines[at][1].startswith("- "):
                items.append(scalar(lines[at][1][2:]))
                at += 1
            return items, at
        mapping = {}
        while at < len(lines) and lines[at][0] == indent:
            key, _, value = lines[at][1].partition(":")
            assert _ == ":" and key and " " not in key, f"not `key: value`: {lines[at][1]}"
            at += 1
            if value.strip():
                mapping[key] = scalar(value)
            else:
                assert at < len(lines) and lines[at][0] > indent, f"'{key}' holds nothing"
                mapping[key], at = block(at, lines[at][0])
        return mapping, at

    parsed, end = block(0, 0)
    assert end == len(lines), f"line {end + 1} is indented less than its block"
    return parsed


def test_manifest_parses_and_names_the_two_scopes_and_the_one_event():
    """Spec 025, acceptance 7."""
    from waku.alwayson.sources import slack

    manifest = _yaml(slack.MANIFEST.read_text(encoding="utf-8"))

    assert manifest["oauth_config"]["scopes"]["bot"] == ["app_mentions:read", "chat:write"]
    assert manifest["oauth_config"]["scopes"].keys() == {"bot"}, "no user scope is asked for"
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == ["app_mention"]
    assert manifest["settings"]["event_subscriptions"].keys() == {"bot_events"}
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert manifest["features"]["bot_user"]["display_name"].strip()
    assert manifest["display_information"]["name"].strip()


def test_manifest_asks_for_what_the_slack_source_and_sender_use():
    """The scopes the manifest grants are the ones `waku connections` checks
    for, so a workspace made from the file passes the check."""
    from waku.alwayson.sources import slack

    manifest = _yaml(slack.MANIFEST.read_text(encoding="utf-8"))

    assert set(manifest["oauth_config"]["scopes"]["bot"]) == set(slack.BOT_SCOPES)
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == [slack.BOT_EVENT]


def test_manifest_ships_inside_the_package_and_holds_no_token():
    from pathlib import Path

    import waku
    from waku.alwayson.sources import slack

    assert Path(waku.__file__).resolve().parent in slack.MANIFEST.resolve().parents
    text = slack.MANIFEST.read_text(encoding="utf-8")
    assert "xoxb-" not in text and "xapp-" not in text
    assert text.isascii()



def test_manifest_turns_interactivity_on_for_the_review_buttons():
    """Spec 035: Approve and Reject arrive over the same Socket Mode
    connection, so interactivity is on and needs no request URL or scope."""
    from waku.alwayson.sources import slack

    manifest = _yaml(slack.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["settings"]["interactivity"] == {"is_enabled": True}
    assert set(manifest["oauth_config"]["scopes"]["bot"]) == set(slack.BOT_SCOPES)

def test_manifest_reader_refuses_a_file_that_is_not_the_shape_it_reads():
    import pytest as _pytest

    with _pytest.raises(AssertionError):
        _yaml("settings:\n  socket_mode_enabled\n")
    with _pytest.raises(AssertionError):
        _yaml("settings:\nfeatures:\n  a: b\n")


# --- a place several people read (2026-11-19) -----------------------------

def test_a_run_from_a_shared_place_is_told_to_leave_personal_memories_out(monkeypatch):
    """Asked in a team's Slack channel for "my latest memories", the hosted
    agent listed its person's outreach drafts to the channel."""
    from waku.alwayson import worker
    from waku.alwayson.events import Event

    monkeypatch.setenv("WAKU_OWNER", "telegram:42")

    def event(source, session, reply_to):
        return Event(id=1, source=source, external_id="e1", session_id=session,
                     payload={"text": "what are my latest memories?",
                              "sender": {"id": "42"}, "reply_to": reply_to},
                     status="pending", received_at="2026-11-19T19:01:00+00:00")

    in_slack = worker.render([event("slack", "slack:C1:1.2",
                                    {"channel": "C1", "thread_ts": "1.2"})])
    in_private = worker.render([event("telegram", "telegram:42", {"chat_id": 42})])

    assert in_slack.endswith(worker.SHARED_PLACE)
    assert "personal memories" in worker.SHARED_PLACE
    assert worker.SHARED_PLACE not in in_private and in_private.endswith(worker.OWNER_READS)


def test_a_chat_reply_is_asked_to_be_short_and_a_github_comment_is_not(monkeypatch):
    """"you talk too much man": a two-line question in Slack got a page of
    headings and lists, and a reminder was "set for 21:26" at 1:26 pm."""
    from waku.alwayson import worker
    from waku.alwayson.events import Event

    def event(source, session, reply_to):
        return Event(id=1, source=source, external_id="e1", session_id=session,
                     payload={"text": "remind me in 2 minutes", "sender": {"id": "7"},
                              "reply_to": reply_to},
                     status="pending", received_at="2026-11-19T21:24:00+00:00")

    in_slack = worker.render([event("slack", "slack:C1:1.2",
                                    {"channel": "C1", "thread_ts": "1.2"})])
    on_github = worker.render([event("github", "github:a/b#1", {"repo": "a/b", "issue": 1})])

    assert worker.CHAT_STYLE in in_slack and "am or pm" in worker.CHAT_STYLE
    assert worker.CHAT_STYLE not in on_github, "an issue comment may be long"
