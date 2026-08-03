# Apple Tools Probe Design

## Goal

Give the Apple Tools connection an honest, side-effect-free health check so a
successful Save or Test connection changes its status from `needs setup` to
`connected`.

## Current Behavior

`apple_tools` is enabled by `WAKU_APPLE_TOOLS`, but it has no registered probe.
Saving therefore invalidates its previous health record without recording a
successful test. The backend returns `installed_but_unconfigured` with
`Configured, not tested`, which the dashboard labels `needs setup`.

## Probe Contract

Add `probe_apple_tools() -> None` to `waku/tools/apple.py`.

- It runs only on macOS through the existing `_osa()` helper.
- It sends one read-only Apple Event to each supported application: Calendar,
  Mail, Reminders, and Notes.
- Each event asks only for the application's version. The probe must not read
  user content or create, update, or delete user data.
- Each application gets an 8-second timeout. The probe continues after an
  individual failure so the final error identifies every unavailable app.
- It returns `None` only when all four applications respond successfully.
- Otherwise it raises `RuntimeError` whose message starts with
  `Apple Tools probe failed:` and includes each failed application and the
  sanitized `_osa()` failure text.

Reading an application version is deliberately the definition of connected:
it verifies that `osascript` can address the app and that macOS Automation
permission is available without making the health check touch personal data.
It does not promise that a large mailbox or calendar will respond quickly to a
real query.

## Integration Flow

Add an `_apple_tools_probe(values)` adapter in `waku/integrations.py` and return
a probe-enabled copy of the integration from `_probed()` when the key is
`apple_tools`.

The existing Connections flow remains authoritative:

1. Save normalizes the enabled flag and runs the probe before persistence.
2. A successful probe saves the configuration, rebuilds an existing browser
   agent as it does today, and records `connected` with `checked_at`.
3. A failed probe leaves the configuration unchanged and returns the detailed
   error with the existing Save anyway option.
4. Save anyway persists the switch but records `Saved without a successful
   test`; it does not claim the integration is connected.
5. Test connection reruns the same probe and moves a recovered connection from
   `error` to `connected`.

The first probe may launch the four Apple applications and cause macOS to show
Automation permission prompts. That is expected user-visible behavior.

## Error Handling

- Non-macOS hosts fail through `_osa()` with the existing macOS-only message.
- Timeouts name the affected application and retain `_osa()`'s actionable
  permission/slow-app explanation.
- A denied application does not prevent the remaining applications from being
  checked.
- The probe reports one integration-level error because the current status
  model has one state per connection; partial per-application status is outside
  this change.

## Test Strategy

Deterministic tests monkeypatch `_osa()` and never launch real Apple apps.

- All four successful version reads return `None`, use an 8-second timeout, and
  contain no data-reading or write commands.
- Mixed failures call all four apps and raise one error naming every failed app.
- Timeout and macOS-only errors remain visible with the relevant app name.
- Connections tests verify that Save and Test connection invoke the adapter,
  record `connected` on success, record `error` on failure, and recover to
  `connected` after a later successful test.
- The focused Apple and integration suites run before the full deterministic
  suite and Ruff.

## Out of Scope

- Changing browser-agent rebuild behavior.
- Changing Supabase, embeddings, or model credentials.
- Creating/deleting synthetic reminders or notes during health checks.
- Reading real email, calendar, reminder, or note content during health checks.
- Adding separate UI statuses for each Apple application.
- Validating `WAKU_APPLE_CALENDARS`; that remains runtime configuration for the
  calendar-reading tool.
