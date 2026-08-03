# Apple Tools Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only health probe that verifies macOS Automation access to Calendar, Mail, Reminders, and Notes and lets the Apple Tools connection report `connected`.

**Architecture:** `waku.tools.apple` owns the platform-specific probe and reuses its bounded `_osa()` runner. `waku.integrations` supplies a thin adapter and attaches it through the existing `_probed()` registry, so Save and Test connection retain the current health persistence, force-save, and recovery semantics.

**Tech Stack:** Python 3.11+, AppleScript through `/usr/bin/osascript`, pytest, Ruff.

## Global Constraints

- The probe is macOS-only and must use the existing `_osa()` helper.
- Probe Calendar, Mail, Reminders, and Notes with `return version`; do not read user content or create, update, or delete data.
- Use an 8-second timeout per application and continue after individual failures.
- Return `None` only when all four applications succeed; otherwise raise one `RuntimeError` naming every failed application.
- Keep the existing one-status-per-integration UI and existing Save anyway behavior.
- Do not change browser-agent rebuild behavior, Supabase/model credentials, `WAKU_APPLE_CALENDARS`, or tool runtime behavior.
- Add no dependencies and no emoji to UI or error text.

---

### Task 1: Implement the bounded, read-only Apple application probe

**Files:**
- Modify: `waku/tools/apple.py:37-64`
- Test: `evals/deterministic/test_apple_tools.py:58-100`

**Interfaces:**
- Consumes: `_osa(script: str, timeout: int = _TIMEOUT) -> tuple[bool, str]`
- Produces: `probe_apple_tools() -> None`
- Produces: `_PROBE_TIMEOUT = 8` and `_PROBE_APPS = ("Calendar", "Mail", "Reminders", "Notes")`

- [ ] **Step 1: Add failing tests for successful, aggregated, timeout, and unsupported-host probes**

Add these tests after `_code_lines()` in `evals/deterministic/test_apple_tools.py`:

```python
def test_probe_apple_tools_checks_all_apps_without_touching_user_data(monkeypatch):
    calls = []

    def osa(script, timeout):
        calls.append((script, timeout))
        return True, "1.0"

    monkeypatch.setattr(apple, "_osa", osa)

    assert apple.probe_apple_tools() is None
    assert calls == [
        (f'tell application "{app}" to return version', 8)
        for app in ("Calendar", "Mail", "Reminders", "Notes")
    ]


def test_probe_apple_tools_aggregates_failures_and_checks_every_app(monkeypatch):
    responses = iter([
        (True, "16.0"),
        (False, "Not authorized"),
        (False, "timed out after 8s"),
        (True, "7.0"),
    ])
    calls = []

    def osa(script, timeout):
        calls.append((script, timeout))
        return next(responses)

    monkeypatch.setattr(apple, "_osa", osa)

    with pytest.raises(RuntimeError) as caught:
        apple.probe_apple_tools()

    message = str(caught.value)
    assert message.startswith("Apple Tools probe failed:")
    assert "Mail: Not authorized" in message
    assert "Reminders: timed out after 8s" in message
    assert "Calendar:" not in message
    assert "Notes:" not in message
    assert len(calls) == 4


@pytest.mark.parametrize(
    "detail",
    [
        "Apple tools are macOS-only.",
        "timed out after 8s. The app may be waiting for permission.",
    ],
)
def test_probe_apple_tools_names_the_app_for_transport_failures(monkeypatch, detail):
    def osa(script, timeout):
        if 'application "Mail"' in script:
            return False, detail
        return True, "1.0"

    monkeypatch.setattr(apple, "_osa", osa)

    with pytest.raises(RuntimeError, match="Mail") as caught:
        apple.probe_apple_tools()

    assert detail in str(caught.value)
```

- [ ] **Step 2: Run the new tests and confirm they fail for the missing interface**

Run:

```bash
.venv/bin/python -m pytest -q evals/deterministic/test_apple_tools.py -k probe_apple_tools
```

Expected: all selected cases fail with `AttributeError: module 'waku.tools.apple' has no attribute 'probe_apple_tools'`.

- [ ] **Step 3: Implement the minimal read-only probe**

Add the constants alongside the existing timeout constants and add the function immediately after `_osa()` in `waku/tools/apple.py`:

```python
_PROBE_TIMEOUT = 8
_PROBE_APPS = ("Calendar", "Mail", "Reminders", "Notes")


def probe_apple_tools() -> None:
    """Verify Automation access to every app without reading or writing user data."""
    failures = []
    for app in _PROBE_APPS:
        ok, detail = _osa(
            f'tell application "{app}" to return version',
            timeout=_PROBE_TIMEOUT,
        )
        if not ok:
            failures.append(f"{app}: {detail}")
    if failures:
        raise RuntimeError("Apple Tools probe failed: " + "; ".join(failures))
```

- [ ] **Step 4: Run the focused Apple Tools suite**

Run:

```bash
.venv/bin/python -m pytest -q evals/deterministic/test_apple_tools.py
```

Expected: PASS, including all new probe cases; tests must not invoke real `osascript`.

- [ ] **Step 5: Commit the probe milestone**

```bash
git add waku/tools/apple.py evals/deterministic/test_apple_tools.py
git commit -m "feat: add read-only Apple Tools health probe" \
  -m "Check Automation access to all four supported Apple apps with bounded, data-free version queries and aggregate actionable failures."
```

### Task 2: Connect the probe to Save and Test connection health

**Files:**
- Modify: `waku/integrations.py:442-480`
- Modify: `evals/deterministic/test_integrations.py:7-168`

**Interfaces:**
- Consumes: `waku.tools.apple.probe_apple_tools() -> None`
- Produces: `_apple_tools_probe(values: Mapping[str, str]) -> None`
- Extends: `_probed(integration: Integration) -> Integration` for key `apple_tools`

- [ ] **Step 1: Import the Apple tool module and add failing Connections tests**

Change the test import in `evals/deterministic/test_integrations.py` to:

```python
from waku.tools import apple, calendar
```

Add these tests after the Apple Calendar connection tests:

```python
def _configure_apple_tools(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setenv("WAKU_APPLE_TOOLS", "1")


def test_apple_tools_save_probes_and_records_connected(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)
    called = []
    monkeypatch.setattr(apple, "probe_apple_tools", lambda: called.append(True))

    result = integrations.apply_integration(
        "apple_tools", {"WAKU_APPLE_TOOLS": "1"}
    )

    assert result.ok
    assert called == [True]
    assert os.environ["WAKU_APPLE_TOOLS"] == "1"
    assert result.view is not None
    assert result.view.status.state is IntegrationState.CONNECTED
    assert result.view.status.checked_at is not None


def test_apple_tools_probe_failure_records_error_and_recovers(monkeypatch, tmp_path):
    _configure_apple_tools(monkeypatch, tmp_path)
    monkeypatch.setattr(
        apple,
        "probe_apple_tools",
        lambda: (_ for _ in ()).throw(RuntimeError("Mail: Not authorized")),
    )

    view = integrations.test_integration("apple_tools")

    assert view.status.state is IntegrationState.ERROR
    assert view.status.message == "Mail: Not authorized"
    assert view.status.checked_at is not None

    monkeypatch.setattr(apple, "probe_apple_tools", lambda: None)
    view = integrations.test_integration("apple_tools")

    assert view.status.state is IntegrationState.CONNECTED
    assert view.status.checked_at is not None


def test_apple_tools_force_save_skips_probe(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations.sys, "platform", "darwin")
    monkeypatch.setattr(browser_agent, "rebuild", lambda: None)

    def unexpected_probe():
        raise AssertionError("force save must skip the probe")

    monkeypatch.setattr(apple, "probe_apple_tools", unexpected_probe)

    result = integrations.apply_integration(
        "apple_tools", {"WAKU_APPLE_TOOLS": "1"}, force=True
    )

    assert result.ok
    assert result.view is not None
    assert result.view.status.state is IntegrationState.ERROR
    assert result.view.status.message == "Saved without a successful test"
```

- [ ] **Step 2: Run the Connections tests and confirm the probe is not wired yet**

Run:

```bash
.venv/bin/python -m pytest -q evals/deterministic/test_integrations.py -k apple_tools
```

Expected: the normal Save/Test cases fail because `apple.probe_apple_tools()` is never called and the status remains `installed_but_unconfigured`; the force-save case may already pass.

- [ ] **Step 3: Add the adapter and register it in `_probed()`**

Add this adapter after `_apple_calendar_probe()` in `waku/integrations.py`:

```python
def _apple_tools_probe(values: Mapping[str, str]) -> None:
    from waku.tools import apple

    apple.probe_apple_tools()
```

Add this branch immediately after the `apple_calendar` branch in `_probed()`:

```python
if integration.key == "apple_tools":
    return Integration(**{**integration.__dict__, "probe": _apple_tools_probe})
```

- [ ] **Step 4: Run focused and full automated verification**

Run each command in order:

```bash
.venv/bin/python -m pytest -q evals/deterministic/test_integrations.py -k apple_tools
.venv/bin/python -m pytest -q evals/deterministic/test_apple_tools.py evals/deterministic/test_integrations.py
.venv/bin/python -m pytest -q evals/deterministic
.venv/bin/python -m ruff check waku evals scripts
git diff --check
```

Expected: every command exits 0; the focused Apple Tools integration cases report PASS, the full deterministic suite has no failures, Ruff reports no violations, and `git diff --check` prints nothing.

- [ ] **Step 5: Verify the real macOS permission flow manually**

Restart the dashboard so it loads the updated Python modules, open Apple Tools, and click Test connection. Approve any Calendar, Mail, Reminders, and Notes Automation prompts.

Expected: no personal content or synthetic data is created; the modal changes to `connected` with a `Last checked` timestamp. If an app is denied, the modal reports that app by name; granting permission in System Settings and testing again changes the status to `connected`.

- [ ] **Step 6: Commit the Connections integration milestone**

```bash
git add waku/integrations.py evals/deterministic/test_integrations.py
git commit -m "feat: report Apple Tools connection health" \
  -m "Wire the read-only multi-app probe into Save and Test connection so successful Automation access records connected and failures remain recoverable."
```

## Acceptance Criteria

- Enabling Apple Tools and completing all four Automation checks changes the dashboard status from `needs setup` to `connected`.
- Test connection never reads user content or creates, updates, or deletes Apple data.
- Failures and timeouts identify the affected application, and all applications are attempted before returning the error.
- A later successful Test connection replaces an error state with `connected`.
- Save anyway retains its existing honest untested/error state.
- Existing Apple tool, connection, deterministic, and lint checks remain green.
