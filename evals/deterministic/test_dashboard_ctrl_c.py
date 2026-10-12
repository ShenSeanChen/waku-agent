"""A Ctrl-C at the dashboard prompt must print a goodbye, not a traceback.

The server parks in serve_forever(), so the first Ctrl-C arrives as
KeyboardInterrupt from inside that call. Every other gateway catches the pair
(see gateway/cli.py, gateway/voice.py, gateway/whatsapp.py) and the user
sees a clean goodbye; the dashboard used to let it escape as a raw traceback.
The goodbye line is pure ASCII on purpose: it has to survive a legacy-codepage
console, unlike the arrows in the startup banner above it (issue #140).
"""

import waku.ops.dashboard as dash


class _InterruptedServer:
    """A stand-in for the parked server: the first Ctrl-C arrives as
    KeyboardInterrupt from inside serve_forever()."""

    def serve_forever(self):
        raise KeyboardInterrupt


def _one_ctrl_c(monkeypatch):
    """Wire main() so it reaches serve_forever() and gets one Ctrl-C there."""
    for var in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "WHATSAPP_TOKEN"):
        monkeypatch.delenv(var, raising=False)  # no gateway may start beside the test
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("WAKU_DASHBOARD_PORT", "7777")
    # keep the module-level registry clean: no stale supervisor callbacks
    monkeypatch.setattr("waku.integrations.register_gateway_status_provider", lambda p: None)
    monkeypatch.setattr("waku.integrations.register_gateway_reloader", lambda r: None)
    monkeypatch.setattr(dash, "ThreadingHTTPServer", lambda *a, **k: _InterruptedServer())


def test_ctrl_c_prints_a_clean_goodbye(monkeypatch, capsys):
    _one_ctrl_c(monkeypatch)
    try:
        dash.main()
    except KeyboardInterrupt:
        raise AssertionError(
            "Ctrl-C escaped main() as a raw traceback; the dashboard owed the "
            "user the same clean goodbye every other gateway prints"
        )
    assert "Dashboard stopped" in capsys.readouterr().out


def test_the_goodbye_survives_a_legacy_codepage_console(monkeypatch, capsys):
    """The startup banner's arrows cannot print on cp1252 (issue #140); the
    goodbye has no such excuse, so it must stay pure ASCII."""
    _one_ctrl_c(monkeypatch)
    try:
        dash.main()
    except KeyboardInterrupt:
        raise AssertionError("Ctrl-C escaped main() as a raw traceback")
    out = capsys.readouterr().out
    line = next((l for l in out.splitlines() if "Dashboard stopped" in l), None)
    assert line is not None, "no goodbye line was printed"
    line.encode("cp1252")  # raises the day someone adds an em-dash to it
