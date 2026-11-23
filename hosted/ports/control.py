"""The control plane. SQLite now; a shared Postgres at more than one VM (4.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Tenant:
    id: str
    sub: str
    email: str
    timezone: str
    status: str
    project_id: int
    created_at: float
    # "person", or "team" for the home of a team space's agent (spec 032). A
    # team tenant has no person behind it: no sign-in, no session, no page.
    kind: str = "person"


@dataclass(frozen=True)
class TeamHome:
    """The home of one team space's agent (spec 032, decision 1): what
    waku.one sent when it made the home, and the tenant that runs it.
    `key_sealed` is the agent's Waku Memory key as SecretBox sealed it: the
    store never sees the key itself."""
    space_id: str
    tenant_id: str
    space_name: str
    agent_id: str
    agent_name: str
    return_url: str
    key_sealed: bytes = field(repr=False)


@dataclass(frozen=True)
class TenantEvent:
    """One event the gateway keeps for a tenant until its container has it
    (spec 026). `source` and `external_id` are the sender's; `session_id` and
    `payload` are what the container's own inbox stores."""
    id: int
    tenant_id: str
    source: str
    external_id: str
    session_id: str
    payload: dict
    status: str           # pending | handed
    received_at: float


@dataclass(frozen=True)
class SlackInstall:
    """One workspace the public Waku Slack app was added to (spec 026 C1).
    `token_sealed` is the bot token as SecretBox sealed it: the store never
    sees the token itself."""
    team_id: str
    tenant_id: str
    team_name: str
    bot_user_id: str
    token_sealed: bytes = field(repr=False)
    # True when the gateway found that its key does not open token_sealed.
    # The person adds the app again, which stores a new token.
    needs_reconnect: bool = False
    # The Slack user who approved the install, or "" when it is not known.
    # Only this person's direct messages are answered (spec 032, version A).
    installer_user_id: str = ""


@dataclass(frozen=True)
class ConnectClient:
    """The gateway's registration with one vendor (spec 029 A1). One row per
    vendor, for every tenant. `client_sealed` holds the client id, the client
    secret and the vendor's sign-in addresses as SecretBox sealed them."""
    server: str
    redirect_uri: str
    client_sealed: bytes = field(repr=False)


@dataclass(frozen=True)
class Connection:
    """One person's sign-in to one vendor (spec 029 A3). `token_sealed` holds
    the access token, the refresh token and when the access token ends, as
    SecretBox sealed them: the store never sees a token."""
    tenant_id: str
    server: str
    token_sealed: bytes = field(repr=False)
    # True when the token cannot be used any more: the key does not open it,
    # or the vendor refused to renew it. The person signs in again.
    needs_reconnect: bool = False


class ControlStore(Protocol):
    def create_tenant(self, *, sub: str, email: str, timezone: str) -> Tenant: ...
    def tenant_by_sub(self, sub: str) -> Tenant | None: ...
    def tenant_by_id(self, tenant_id: str) -> Tenant | None: ...
    def tenant_by_email(self, email: str) -> Tenant | None: ...
    def set_status(self, tenant_id: str, status: str) -> None: ...
    def set_timezone(self, tenant_id: str, timezone: str) -> None: ...
    def create_session(self, *, tenant_id: str, value: str, expires_at: float) -> None: ...
    def session_tenant(self, value: str, now: float) -> str | None: ...
    def delete_sessions(self, tenant_id: str) -> None: ...
    def issue_token(self, tenant_id: str) -> str: ...
    def revoke_tokens(self, tenant_id: str) -> None: ...
    def has_live_token(self, tenant_id: str) -> bool: ...
    def tenant_for_token_hash(self, digest: str) -> tuple[str, str] | None: ...
    def set_memory_key(self, tenant_id: str, *, key: str, key_id: str) -> None: ...
    def memory_key(self, tenant_id: str) -> str: ...
    def memory_key_id(self, tenant_id: str) -> str: ...
    def add_event(self, tenant_id: str, *, source: str, external_id: str,
                  session_id: str, payload: dict) -> bool: ...
    def pending_events(self, tenant_id: str) -> list[TenantEvent]: ...
    def has_session(self, tenant_id: str, session_id: str) -> bool: ...
    def has_slack_channel(self, tenant_id: str, channel: str) -> bool: ...
    def tenants_with_pending_events(self) -> list[str]: ...
    def mark_events_handed(self, tenant_id: str, event_ids: list[int]) -> None: ...
    def set_wakeup(self, tenant_id: str, due_at: float) -> None: ...
    def clear_wakeup(self, tenant_id: str) -> None: ...
    def wakeup(self, tenant_id: str) -> float | None: ...
    def due_wakeups(self, now: float) -> list[str]: ...
    def set_slack_install(self, tenant_id: str, *, team_id: str, team_name: str,
                          bot_user_id: str, token_sealed: bytes,
                          installer_user_id: str = "") -> bool: ...
    def slack_tenant(self, team_id: str) -> str | None: ...
    def slack_install(self, tenant_id: str) -> SlackInstall | None: ...
    def slack_installs(self) -> list[SlackInstall]: ...
    def set_slack_reconnect(self, team_id: str, needed: bool) -> None: ...
    def delete_slack_install(self, team_id: str) -> None: ...
    def set_connect_client(self, server: str, *, redirect_uri: str,
                           client_sealed: bytes) -> None: ...
    def connect_client(self, server: str) -> ConnectClient | None: ...
    def set_connection(self, tenant_id: str, server: str, *,
                       token_sealed: bytes) -> None: ...
    def connection(self, tenant_id: str, server: str) -> Connection | None: ...
    def connections(self, tenant_id: str) -> list[Connection]: ...
    def set_connection_reconnect(self, tenant_id: str, server: str,
                                 needed: bool) -> None: ...
    def delete_connection(self, tenant_id: str, server: str) -> bool: ...
    def set_team_home(self, *, space_id: str, space_name: str, agent_id: str,
                      agent_name: str, return_url: str,
                      key_sealed: bytes) -> tuple[TeamHome, bool]: ...
    def team_home(self, space_id: str) -> TeamHome | None: ...
    def team_home_for_tenant(self, tenant_id: str) -> TeamHome | None: ...
    def delete_tenant(self, tenant_id: str) -> None: ...
