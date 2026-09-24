"""The dashboard's bind address — loopback unless told otherwise."""

from __future__ import annotations

import pytest

import waku.ops.dashboard as dash


def test_the_default_is_loopback(monkeypatch):
    monkeypatch.delenv("WAKU_DASHBOARD_HOST", raising=False)
    assert dash.bind_host() == "127.0.0.1"


def test_the_variable_changes_it(monkeypatch):
    monkeypatch.setenv("WAKU_DASHBOARD_HOST", "0.0.0.0")
    assert dash.bind_host() == "0.0.0.0"


def test_a_non_loopback_address_warns(monkeypatch, capsys):
    """The SQL console has no authentication. Leaving loopback is a choice
    that has to be visible in the terminal that made it."""
    monkeypatch.setenv("WAKU_DASHBOARD_HOST", "0.0.0.0")
    dash.bind_host()
    warning = capsys.readouterr().out
    assert "WAKU_DASHBOARD_HOST" in warning
    assert "0.0.0.0" in warning


def test_loopback_forms_do_not_warn(monkeypatch, capsys):
    for value in ("127.0.0.1", "::1", "localhost"):
        monkeypatch.setenv("WAKU_DASHBOARD_HOST", value)
        dash.bind_host()
        assert capsys.readouterr().out == "", f"{value} should not warn"


def test_the_warning_prints_once_however_many_ports_are_busy(monkeypatch, capsys):
    """main() walks ten ports past a busy one. The warning is about the
    environment, which cannot change between iterations, so printing it per
    attempt says nothing new and trains people to scroll past the one line
    that is the whole point of letting the dashboard leave loopback."""
    monkeypatch.setenv("WAKU_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("WAKU_DASHBOARD_PORT", "7777")

    def every_port_busy(*args, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(dash, "ThreadingHTTPServer", every_port_busy)
    with pytest.raises(SystemExit):
        dash.main()

    out = capsys.readouterr().out
    assert out.count("WAKU_DASHBOARD_HOST=0.0.0.0") == 1, (
        f"the security warning printed {out.count('WAKU_DASHBOARD_HOST=0.0.0.0')} "
        f"times across the port walk; it must print once"
    )
    assert out.count("busy, trying") == 10, (
        "expected the walk itself to still report each busy port"
    )
