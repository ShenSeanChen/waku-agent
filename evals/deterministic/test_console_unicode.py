"""DETERMINISTIC EVAL — Windows pipe safety for the gateways.

Waku prints model output that can hold any unicode (→, ✓, emoji). On Windows a
redirected stdout defaults to the console code page (cp1251 on RU systems) and
UnicodeEncodeError would kill the process — seen live: `waku dashboard` under a
pipe died at its own "→" banner, and the Telegram poller died printing a reply.
make_console_unicode_safe() swaps errors='replace' on the streams so no exotic
character can take a gateway down. These tests pin that contract.
"""

from waku.gateway.cli import make_console_unicode_safe


class _FakeStream:
    def __init__(self):
        self.reconfigured = []

    def reconfigure(self, **kwargs):
        self.reconfigured.append(kwargs)


def test_reconfigure_sets_replace_on_every_usable_stream(monkeypatch):
    stdout, stderr = _FakeStream(), _FakeStream()
    monkeypatch.setattr("waku.gateway.cli.sys.stdout", stdout)
    monkeypatch.setattr("waku.gateway.cli.sys.stderr", stderr)

    make_console_unicode_safe()

    assert stdout.reconfigured == [{"errors": "replace"}]
    assert stderr.reconfigured == [{"errors": "replace"}]


def test_streams_without_reconfigure_are_left_alone(monkeypatch):
    class Plain:  # e.g. a pytest capture object without reconfigure
        pass

    plain = Plain()
    monkeypatch.setattr("waku.gateway.cli.sys.stdout", plain)
    monkeypatch.setattr("waku.gateway.cli.sys.stderr", plain)

    make_console_unicode_safe()  # must not raise


def test_reconfigure_failure_is_tolerated(monkeypatch):
    class Broken:  # e.g. a closed stream mid-shutdown
        def reconfigure(self, **kwargs):
            raise ValueError("stream closed")

    broken = Broken()
    monkeypatch.setattr("waku.gateway.cli.sys.stdout", broken)
    monkeypatch.setattr("waku.gateway.cli.sys.stderr", broken)

    make_console_unicode_safe()  # must not raise
