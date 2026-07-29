"""GatewaySupervisor lifecycle tests without optional gateway libraries."""

from __future__ import annotations

import os

from waku.gateway.supervisor import GatewaySupervisor
from waku.integrations import IntegrationState, IntegrationStatus


class Handle:
    def __init__(self, state=IntegrationState.CONNECTED):
        self.stopped = False
        self.state = state

    def stop(self):
        self.stopped = True

    def status(self):
        return IntegrationStatus(self.state)


def test_reconcile_restart_remove_and_shutdown(monkeypatch, tmp_path):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    calls, handles = [], []
    def start():
        calls.append(1)
        handle = Handle()
        handles.append(handle)
        return handle
    supervisor = GatewaySupervisor({"telegram": start}, {"telegram": ("TELEGRAM_BOT_TOKEN",)})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "one")
    assert supervisor.reconcile()["telegram"].state is IntegrationState.CONNECTED
    supervisor.reconcile()
    assert len(calls) == 1
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "two")
    supervisor.reconcile()
    assert len(calls) == 2 and handles[0].stopped
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    supervisor.reconcile()
    assert handles[1].stopped and supervisor.status("telegram") is None


def test_failed_restart_restores_old_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("WAKU_HOME", str(tmp_path))
    starts = []
    def start():
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        starts.append(token)
        if token == "bad":
            return None
        return Handle()
    supervisor = GatewaySupervisor({"telegram": start}, {"telegram": ("TELEGRAM_BOT_TOKEN",)})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "good")
    supervisor.reconcile()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bad")
    outcome = supervisor.reconcile()
    assert outcome["telegram"].state is IntegrationState.ERROR
    assert starts == ["good", "bad", "good"]
    assert supervisor.status("telegram").state is IntegrationState.CONNECTED
