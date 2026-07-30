"""Lifecycle manager for background gateways.

Adapters return a small handle with ``stop()`` and ``status()``.  Keeping this
module adapter-agnostic makes its failure and rollback behaviour testable
without optional gateway dependencies.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from typing import Protocol

from waku.integrations import IntegrationState, IntegrationStatus, record_health


class GatewayHandle(Protocol):
    def stop(self) -> None: ...
    def status(self) -> IntegrationStatus: ...


class GatewaySupervisor:
    def __init__(self, starters: dict[str, Callable[[], GatewayHandle | None]],
                 env_fields: dict[str, tuple[str, ...]]) -> None:
        self._starters = starters
        self._env_fields = env_fields
        self._handles: dict[str, GatewayHandle] = {}
        self._fingerprints: dict[str, str] = {}
        self._snapshots: dict[str, dict[str, str | None]] = {}

    def _fingerprint(self, key: str) -> str:
        values = "\0".join(os.environ.get(name, "") for name in self._env_fields[key])
        return hashlib.sha256(values.encode()).hexdigest()

    def _configured(self, key: str) -> bool:
        # Registry declares the token as the first gateway field.
        fields = self._env_fields[key]
        return bool(fields and os.environ.get(fields[0]))

    def _environment(self, key: str) -> dict[str, str | None]:
        return {field: os.environ.get(field) for field in self._env_fields[key]}

    @staticmethod
    def _restore_environment(snapshot: dict[str, str | None]) -> None:
        for field, value in snapshot.items():
            if value is None:
                os.environ.pop(field, None)
            else:
                os.environ[field] = value

    def status(self, key: str) -> IntegrationStatus | None:
        handle = self._handles.get(key)
        if handle is None:
            return None
        try:
            return handle.status()
        except Exception as exc:
            return IntegrationStatus(IntegrationState.ERROR, str(exc))

    def reconcile(self, keys: set[str] | None = None) -> dict[str, IntegrationStatus]:
        result: dict[str, IntegrationStatus] = {}
        for key in keys or set(self._starters):
            old = self._handles.get(key)
            try:
                if not self._configured(key):
                    if old:
                        old.stop()
                    self._handles.pop(key, None)
                    self._fingerprints.pop(key, None)
                    self._snapshots.pop(key, None)
                    continue
                fingerprint = self._fingerprint(key)
                if old and self._fingerprints.get(key) == fingerprint:
                    result[key] = old.status()
                    continue
                if old:
                    old.stop()
                handle = self._starters[key]()
                if handle is None:
                    raise RuntimeError("gateway failed to start")
                self._handles[key] = handle
                self._fingerprints[key] = fingerprint
                self._snapshots[key] = self._environment(key)
                result[key] = handle.status()
            except Exception as exc:
                # Gateway tokens cannot run concurrently, so restart is
                # necessarily stop-then-start. If the new instance fails,
                # temporarily restore the exact old environment and bring the
                # old instance back before returning the error to the caller.
                old_snapshot = self._snapshots.get(key)
                desired = self._environment(key)
                self._handles.pop(key, None)
                self._fingerprints.pop(key, None)
                if old_snapshot:
                    try:
                        self._restore_environment(old_snapshot)
                        restored = self._starters[key]()
                        if restored is not None:
                            self._handles[key] = restored
                            self._fingerprints[key] = self._fingerprint(key)
                    except Exception:
                        pass
                    finally:
                        self._restore_environment(desired)
                result[key] = IntegrationStatus(IntegrationState.ERROR, str(exc))
            record_health(key, result[key])
        return result

    def shutdown(self) -> None:
        for handle in tuple(self._handles.values()):
            try:
                handle.stop()
            except Exception:
                pass
        self._handles.clear()
        self._fingerprints.clear()
        self._snapshots.clear()
