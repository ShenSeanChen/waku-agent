"""Every value config/gateway.env carries, and nothing else.

PINNED IN BOTH DIRECTIONS, the way hosted/spawner/template.ENV_NAMES is:
config_from_env raises on a missing required name, and
test_gateway.py::test_the_example_file_and_the_config_agree asserts that
hosted/deploy/gateway.env.example names exactly these and no others. Without
the second half a misspelling in F1's install.sh is silent -- the operator
sets a value and the gateway ignores it.

THE SUPABASE VALUES ARE PUBLIC. The JWKS holds one ES256 public key and the
publishable key is meant for a browser. The gateway holds no Supabase secret
at all.

THE FOUR SLACK VALUES ARE NOT (spec 026 C). The client secret, the signing
secret and the key that seals each workspace's bot token in control.db are
secrets, so config/gateway.env is one now. install.sh has always written it
0600 in a directory only root can enter, and Compose hands it to the gateway
as an env_file; nothing reads it from the repository. SlackSettings keeps all
three out of its repr, and no message in this module quotes a value.

SO IS WAKU_AGENT_PLATFORM_SECRET (spec 032): the one secret waku.one and this
gateway both hold. Whoever has it can make a team home here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hosted.core import idle
from hosted.gateway.secretbox import SecretBox

# The live project's audience. It is NOT "authenticated": that is the `role`
# claim. See hosted/gateway/identity.py's docstring.
DEFAULT_AUDIENCE = "https://api.waku.one/mcp"

REQUIRED_ENV_NAMES = (
    "WAKU_APEX_HOST",
    "WAKU_GATEWAY_BIND",
    "WAKU_GATEWAY_PORT",
    "WAKU_CONTROL_DB",
    "WAKU_SPAWNER_SOCKET",
    "WAKU_GATEWAY_SOCKET",
    "WAKU_PROXY_SOCKET",
    "WAKU_ADMIN_SOCKET",
    "WAKU_MAX_RUNNING",
    "WAKU_SUPABASE_URL",
    "WAKU_SUPABASE_ISSUER",
    "WAKU_SUPABASE_JWKS_URL",
    "WAKU_SUPABASE_AUDIENCE",
    "WAKU_SUPABASE_PUBLISHABLE_KEY",
    # Read by hosted.core.quota.plans_from_env, which has its own defaults (30
    # and 120) -- and REQUIRED anyway. An earlier draft made these two
    # optional on the reasoning that the code works without them. That is the
    # reasoning REQUIRED_ENV_NAMES exists to refuse: `WAKU_FRE_TURNS_PER_HOUR`
    # in install.sh would set nothing, raise nothing, and silently run the
    # whole VM on the default. The example-file pin catches a name missing
    # from the file; only this catches a name misspelt in the file.
    "WAKU_FREE_TURNS_PER_HOUR",
    "WAKU_BYOK_TURNS_PER_HOUR",
    # Spec 008 D: the pages allowed to frame the agent's chat. In
    # MAY_BE_ABSENT below.
    "WAKU_EMBED_ORIGINS",
    # Spec 001 E4: minutes with nothing in flight and no non-background
    # request before the idle loop stops a tenant's container. In
    # MAY_BE_ABSENT below; absent means idle.IDLE_SECONDS, fifteen minutes.
    "WAKU_IDLE_MINUTES",
    # Spec 026 C: the public Waku Slack app. SLACK_ENV_NAMES below, and in
    # MAY_BE_ABSENT: without all four the gateway starts and its Slack routes
    # refuse, naming what is missing.
    "WAKU_SLACK_CLIENT_ID",
    "WAKU_SLACK_CLIENT_SECRET",
    "WAKU_SLACK_SIGNING_SECRET",
    "WAKU_SLACK_TOKEN_KEY",
    # Spec 029 F: the GitHub App a person signs in to GitHub through.
    # GITHUB_ENV_NAMES below, and in MAY_BE_ABSENT: without both, GitHub is
    # not a connector on this server.
    "WAKU_GITHUB_CLIENT_ID",
    "WAKU_GITHUB_CLIENT_SECRET",
    # Spec 031 A: the Google client a person signs in to Gmail through.
    # GOOGLE_ENV_NAMES below, and in MAY_BE_ABSENT: without both, Gmail is
    # not a connector on this server and its agents have no mail tools.
    "WAKU_GOOGLE_CLIENT_ID",
    "WAKU_GOOGLE_CLIENT_SECRET",
    # Spec 032: the secret waku.one sends with each call to /v1/team-homes.
    # In MAY_BE_ABSENT: without it the gateway starts and those routes
    # answer 503.
    "WAKU_AGENT_PLATFORM_SECRET",
)

# The Slack app's client id, client secret and signing secret, which the
# operator copies from the app's settings page at api.slack.com, and the key
# that seals each workspace's bot token, which install.sh generates on the VM.
SLACK_ENV_NAMES = ("WAKU_SLACK_CLIENT_ID", "WAKU_SLACK_CLIENT_SECRET",
                   "WAKU_SLACK_SIGNING_SECRET", "WAKU_SLACK_TOKEN_KEY")

# WRITTEN BY install.sh, PINNED LIKE THE REST, AND STILL ALLOWED TO BE ABSENT,
# on the proxy's precedent for its treg names (hosted/proxy/config.py).
# upgrade.sh never writes config/, so a gateway.env written before spec 008
# has no WAKU_EMBED_ORIGINS line, and a gateway that refused to start without
# it would take every tenant down on the upgrade that added the embedded chat.
# Absent or empty, the allowlist is DEFAULT_EMBED_ORIGINS.
MAY_BE_ABSENT = frozenset({"WAKU_EMBED_ORIGINS", "WAKU_IDLE_MINUTES", *SLACK_ENV_NAMES,
                           "WAKU_GITHUB_CLIENT_ID", "WAKU_GITHUB_CLIENT_SECRET",
                           "WAKU_GOOGLE_CLIENT_ID", "WAKU_GOOGLE_CLIENT_SECRET",
                           "WAKU_AGENT_PLATFORM_SECRET"})

# waku.one's three hosts. http://localhost:3000, for developing waku.one's own
# page against a real gateway, is never in the default: an operator who wants
# it writes the whole list, that origin included.
DEFAULT_EMBED_ORIGINS = ("https://www.waku.one", "https://waku.one",
                         "https://dev.waku.one")

# ONE ORIGIN, AND NOTHING THAT IS NOT AN ORIGIN. Every value lands verbatim in
# a Content-Security-Policy header and in a postMessage target, so a path, a
# wildcard, a quote or a semicolon is refused at startup rather than becoming
# a second directive. https for any host; plain http only for loopback, which
# is what a developer's own waku.one runs on.
_EMBED_ORIGIN = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+(?::[0-9]{1,5})?"
    r"|http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?")


def embed_origins_from(raw: str | None) -> tuple[str, ...]:
    """WAKU_EMBED_ORIGINS as a tuple of origins, or the default when it is
    absent or empty. Separated by spaces, the way a CSP source list is, or by
    commas; a value that is not one exact origin raises."""
    values = tuple(v for v in re.split(r"[\s,]+", (raw or "").strip()) if v)
    if not values:
        return DEFAULT_EMBED_ORIGINS
    bad = [v for v in values if not _EMBED_ORIGIN.fullmatch(v)]
    if bad:
        raise ValueError(
            f"WAKU_EMBED_ORIGINS holds {bad}, which are not origins. Write each "
            "as https://host (or http://localhost:<port>), lowercase, with no "
            "path, separated by spaces.")
    return values


def idle_seconds_from(raw: str | None) -> int:
    """WAKU_IDLE_MINUTES in seconds, or idle.IDLE_SECONDS when it is absent or
    empty. A whole number of at least 1; anything else raises at startup,
    because a typo here would otherwise either stop every container a minute
    after its last message or never stop one at all."""
    value = (raw or "").strip()
    if not value:
        return idle.IDLE_SECONDS
    if not value.isdigit() or int(value) < 1:
        raise ValueError(
            f"WAKU_IDLE_MINUTES is {raw!r}. Write a whole number of minutes, "
            "at least 1, or leave the line out for the default of 15.")
    return int(value) * 60


@dataclass(frozen=True)
class SlackSettings:
    """The public Slack app's credentials. Only the client id is public: it
    is in the address "Add to Slack" opens."""
    client_id: str
    client_secret: str = field(repr=False)
    signing_secret: str = field(repr=False)
    token_key: bytes = field(repr=False)


def slack_from(env: Mapping[str, str]) -> tuple[SlackSettings | None, str]:
    """(the settings, "") when all four values are usable, or (None, why not).

    The reason names every missing name and quotes no value. It never raises:
    a gateway.env written before spec 026 has none of the four, and a typo in
    one of them must not stop the gateway that serves every tenant."""
    values = {name: (env.get(name) or "").strip() for name in SLACK_ENV_NAMES}
    missing = [name for name, value in values.items() if not value]
    if missing:
        return None, f"config/gateway.env is missing {', '.join(missing)}"
    try:
        SecretBox.from_hex(values["WAKU_SLACK_TOKEN_KEY"])
    except ValueError as exc:
        return None, f"WAKU_SLACK_TOKEN_KEY in config/gateway.env is not usable: {exc}"
    return SlackSettings(
        client_id=values["WAKU_SLACK_CLIENT_ID"],
        client_secret=values["WAKU_SLACK_CLIENT_SECRET"],
        signing_secret=values["WAKU_SLACK_SIGNING_SECRET"],
        token_key=bytes.fromhex(values["WAKU_SLACK_TOKEN_KEY"])), ""


def connect_key_from(env: Mapping[str, str]) -> tuple[bytes | None, str]:
    """(the key that seals a connector's tokens, "") or (None, why not).

    Spec 029 seals each person's Notion or Linear tokens with the key that
    already seals a workspace's Slack token, WAKU_SLACK_TOKEN_KEY. It is one
    key because it has one job, sealing what control.db must not hold in the
    clear, and because install.sh already writes it: connectors need no new
    line in config/gateway.env. Each sealed value is bound to its own row, so
    a Slack token does not open as a connector's. Connectors need this one
    value and none of the other three Slack values. It never raises, for
    slack_from's reason."""
    value = (env.get("WAKU_SLACK_TOKEN_KEY") or "").strip()
    if not value:
        return None, "config/gateway.env is missing WAKU_SLACK_TOKEN_KEY"
    try:
        SecretBox.from_hex(value)
    except ValueError as exc:
        return None, f"WAKU_SLACK_TOKEN_KEY in config/gateway.env is not usable: {exc}"
    return bytes.fromhex(value), ""


PLATFORM_SECRET_ENV = "WAKU_AGENT_PLATFORM_SECRET"
# The fewest characters the shared secret may have. `openssl rand -hex 32`
# writes 64. A shorter value is a typo or a placeholder, and a guessable one
# would let any caller hand the gateway a key and get a home for it.
PLATFORM_SECRET_MIN_CHARS = 32


def platform_secret_from(env: Mapping[str, str]) -> tuple[str, str]:
    """(the secret waku.one's calls must carry, "") or ("", why not).

    THE SAME VALUE IS SET ON waku.one, as its own WAKU_AGENT_PLATFORM_SECRET
    (spec 032, decision 2). The reason names the variable and quotes no
    value. It never raises, for slack_from's reason: a gateway.env from
    before spec 032 has no such line."""
    value = (env.get(PLATFORM_SECRET_ENV) or "").strip()
    if not value:
        return "", f"config/gateway.env is missing {PLATFORM_SECRET_ENV}"
    if len(value) < PLATFORM_SECRET_MIN_CHARS or any(ch.isspace() for ch in value):
        return "", (f"{PLATFORM_SECRET_ENV} in config/gateway.env is not usable: write "
                    f"at least {PLATFORM_SECRET_MIN_CHARS} characters with no space")
    return value, ""


# What a GatewayConfig built by hand says about team homes: off, no secret.
PLATFORM_NOT_CONFIGURED = f"config/gateway.env is missing {PLATFORM_SECRET_ENV}"

# The client id and client secret of the GitHub App, which the operator
# copies from the app's page at github.com (hosted/README.md, "GitHub").
GITHUB_ENV_NAMES = ("WAKU_GITHUB_CLIENT_ID", "WAKU_GITHUB_CLIENT_SECRET")
# A client id is letters, digits and dots ("Iv23li..."), and a secret is hex.
# A value with anything else in it is a line pasted wrong.
_CLIENT_VALUE = re.compile(r"[A-Za-z0-9._-]{8,128}")


def connect_clients_from(env: Mapping[str, str]) -> tuple[dict[str, tuple[str, str]], str]:
    """({connector name: (client id, client secret)}, "") for each vendor
    that takes sign-in only from a client registered beforehand, or ({}, why
    GitHub is not among them). GitHub is the one such vendor (spec 029 F).

    The reason names a name and quotes no value. It never raises, for
    slack_from's reason: a typo here must cost GitHub and nothing else."""
    values = {name: (env.get(name) or "").strip() for name in GITHUB_ENV_NAMES}
    missing = [name for name, value in values.items() if not value]
    if missing:
        return {}, f"config/gateway.env is missing {', '.join(missing)}"
    bad = [name for name, value in values.items() if not _CLIENT_VALUE.fullmatch(value)]
    if bad:
        return {}, f"{', '.join(bad)} in config/gateway.env is not a value GitHub issues"
    return {"github": (values["WAKU_GITHUB_CLIENT_ID"],
                       values["WAKU_GITHUB_CLIENT_SECRET"])}, ""


# The client id and client secret of the Google OAuth client, which the
# operator copies from the Google Cloud console (hosted/README.md, "Gmail").
GOOGLE_ENV_NAMES = ("WAKU_GOOGLE_CLIENT_ID", "WAKU_GOOGLE_CLIENT_SECRET")


def google_client_from(env: Mapping[str, str]) -> tuple[dict[str, tuple[str, str]], str]:
    """({"gmail": (client id, client secret)}, "") or ({}, why Gmail is not
    offered) (spec 031 A). A Google client id ends in
    .apps.googleusercontent.com and a secret is letters, digits and dashes,
    so both fit the shape a GitHub value has.

    The reason names a name and quotes no value. It never raises, for
    slack_from's reason: a typo here must cost Gmail and nothing else."""
    values = {name: (env.get(name) or "").strip() for name in GOOGLE_ENV_NAMES}
    missing = [name for name, value in values.items() if not value]
    if missing:
        return {}, f"config/gateway.env is missing {', '.join(missing)}"
    bad = [name for name, value in values.items() if not _CLIENT_VALUE.fullmatch(value)]
    if bad:
        return {}, f"{', '.join(bad)} in config/gateway.env is not a value Google issues"
    return {"gmail": (values["WAKU_GOOGLE_CLIENT_ID"],
                      values["WAKU_GOOGLE_CLIENT_SECRET"])}, ""


# What a GatewayConfig built by hand says about Gmail: off, both missing.
GOOGLE_NOT_CONFIGURED = f"config/gateway.env is missing {', '.join(GOOGLE_ENV_NAMES)}"

# What a GatewayConfig built by hand says about GitHub: off, both missing.
GITHUB_NOT_CONFIGURED = f"config/gateway.env is missing {', '.join(GITHUB_ENV_NAMES)}"

# What a GatewayConfig built by hand says about connectors: off, no key.
CONNECT_NOT_CONFIGURED = "config/gateway.env is missing WAKU_SLACK_TOKEN_KEY"

# What a GatewayConfig built by hand says about Slack: off, all four missing.
SLACK_NOT_CONFIGURED = f"config/gateway.env is missing {', '.join(SLACK_ENV_NAMES)}"


@dataclass(frozen=True)
class GatewayConfig:
    apex_host: str
    bind_host: str
    port: int
    control_db: Path
    spawner_socket: Path
    gateway_socket: Path
    proxy_socket: Path
    admin_socket: Path
    max_running: int
    supabase_url: str
    supabase_issuer: str
    supabase_jwks_url: str
    supabase_audience: str
    supabase_publishable_key: str
    # Defaulted so every place that builds a GatewayConfig by hand -- the
    # tests, the Docker tier -- gets the documented allowlist without naming it.
    embed_origins: tuple[str, ...] = DEFAULT_EMBED_ORIGINS
    idle_seconds: int = idle.IDLE_SECONDS
    # Spec 026 C. `slack` is None when the Slack routes are off, and
    # `slack_problem` then says why in words an operator can act on.
    slack: SlackSettings | None = None
    slack_problem: str = SLACK_NOT_CONFIGURED
    # Spec 029. `connect_key` is None when the connector routes are off, and
    # `connect_problem` then says why.
    connect_key: bytes | None = field(default=None, repr=False)
    connect_problem: str = CONNECT_NOT_CONFIGURED
    # Spec 029 F. The client each connector that needs one signs in with, by
    # connector name: {"github": (client id, client secret)}, or empty, and
    # `github_problem` then says why GitHub is not offered. Spec 031 A adds
    # "gmail" to the same mapping, and `google_problem`.
    connect_clients: Mapping[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    github_problem: str = GITHUB_NOT_CONFIGURED
    google_problem: str = GOOGLE_NOT_CONFIGURED
    # Spec 032. `platform_secret` is "" when the team-home routes are off,
    # and `platform_problem` then says why.
    platform_secret: str = field(default="", repr=False)
    platform_problem: str = PLATFORM_NOT_CONFIGURED


def config_from_env(env: Mapping[str, str]) -> GatewayConfig:
    missing = [name for name in REQUIRED_ENV_NAMES
               if name not in MAY_BE_ABSENT and not env.get(name)]
    if missing:
        raise ValueError(
            f"config/gateway.env is missing {missing}. install.sh writes this "
            "file; every name in config.REQUIRED_ENV_NAMES must be in it.")
    slack, slack_problem = slack_from(env)
    connect_key, connect_problem = connect_key_from(env)
    connect_clients, github_problem = connect_clients_from(env)
    google_client, google_problem = google_client_from(env)
    platform_secret, platform_problem = platform_secret_from(env)
    return GatewayConfig(
        apex_host=env["WAKU_APEX_HOST"].strip().lower(),
        bind_host=env["WAKU_GATEWAY_BIND"],
        port=int(env["WAKU_GATEWAY_PORT"]),
        control_db=Path(env["WAKU_CONTROL_DB"]),
        spawner_socket=Path(env["WAKU_SPAWNER_SOCKET"]),
        gateway_socket=Path(env["WAKU_GATEWAY_SOCKET"]),
        proxy_socket=Path(env["WAKU_PROXY_SOCKET"]),
        admin_socket=Path(env["WAKU_ADMIN_SOCKET"]),
        max_running=int(env["WAKU_MAX_RUNNING"]),
        supabase_url=env["WAKU_SUPABASE_URL"].rstrip("/"),
        supabase_issuer=env["WAKU_SUPABASE_ISSUER"].rstrip("/"),
        supabase_jwks_url=env["WAKU_SUPABASE_JWKS_URL"],
        supabase_audience=env["WAKU_SUPABASE_AUDIENCE"],
        supabase_publishable_key=env["WAKU_SUPABASE_PUBLISHABLE_KEY"],
        embed_origins=embed_origins_from(env.get("WAKU_EMBED_ORIGINS")),
        idle_seconds=idle_seconds_from(env.get("WAKU_IDLE_MINUTES")),
        slack=slack,
        slack_problem=slack_problem,
        connect_key=connect_key,
        connect_problem=connect_problem,
        connect_clients={**connect_clients, **google_client},
        github_problem=github_problem,
        google_problem=google_problem,
        platform_secret=platform_secret,
        platform_problem=platform_problem,
    )
