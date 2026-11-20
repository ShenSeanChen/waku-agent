"""The connectors a person on hosted Waku can sign in to (spec 029).

Notion, Linear and GitHub. Each is the vendor's own hosted MCP server.

Notion and Linear take sign-in from a client that registers itself (dynamic
client registration). GitHub does not: its server takes sign-in only from a
GitHub App registered with GitHub beforehand, so its entry names GitHub's two
sign-in addresses, and the gateway offers it only when it holds that app's
client id and secret (hosted/gateway/config.py).

A person cannot add another address. The gateway calls a connector with the
platform's network position, so the list is closed and changes by pull
request.

THIS IS A SECOND COPY OF THREE ENTRIES OF waku/connectors.toml, the list the
local agent uses (spec 025). hosted/ and waku/ never import each other, and
the services image has no waku/ in it, so the address and the `writes`
patterns are written here again. evals/deterministic/hosted/
test_connect_vendors.py fails when the two copies differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase


@dataclass(frozen=True)
class Vendor:
    """One connector.

      name    the server's name in the container's mcp.json, and in every
              route: /connect/start?server=<name>, /connect/<name>/mcp
      title   what a person reads
      url     the vendor's MCP address. Its origin is the only origin the
              gateway talks to for this vendor: discovery, registration, the
              sign-in page and the token all have to be on it
      scope   what the sign-in asks for
      writes  patterns of the tool names that change something, matched the
              way a shell matches a file name. Such a tool does not run on
              hosted Waku yet: nobody there can be asked first (spec 029,
              group C)

    The last three are for a vendor that takes sign-in only from a client
    registered with it beforehand, and are empty for every other vendor:

      authorize_url  the vendor's sign-in page
      token_url      where a code, and later a refresh token, is exchanged
      issuer         the name the vendor's sign-in server gives itself, which
                     it may send back as `iss` on the callback. Empty means
                     the origin of authorize_url
      note           one sentence for the setup page about what the agent
                     can reach there

    Both addresses are from the vendor's documentation. Nothing the vendor
    answers at run time moves them.
    """
    name: str
    title: str
    url: str
    scope: str
    writes: tuple[str, ...]
    authorize_url: str = ""
    token_url: str = ""
    issuer: str = ""
    note: str = ""

    def is_write(self, tool: str) -> bool:
        return any(fnmatchcase(tool, pattern) for pattern in self.writes)


VENDORS: dict[str, Vendor] = {vendor.name: vendor for vendor in (
    Vendor(
        name="notion", title="Notion", url="https://mcp.notion.com/mcp",
        # The one scope Notion's server names in its 401 answer.
        scope="default",
        writes=(
            "notion-create-*", "notion-update-*", "notion-delete-*", "notion-move-*",
            "notion-duplicate-*", "notion-restore-*", "notion-set-*", "notion-send-*",
            "notion-spawn-*", "notion-stop-*", "notion-upload-*", "notion-convert-*",
            "notion-link-*", "notion-resolve-*", "notion-complete-*",
            "notion-calendar-create-*", "notion-calendar-update-*",
            "notion-calendar-cancel-*")),
    Vendor(
        name="linear", title="Linear", url="https://mcp.linear.app/mcp",
        # `read` alone, while a write cannot be approved on hosted Waku. The
        # person signs in again for `write` when approvals are built.
        scope="read",
        writes=(
            "create_*", "save_*", "update_*", "delete_*", "remove_*", "add_*",
            "archive_*", "retire_*", "restore_*", "merge_*", "resolve_*", "submit_*",
            "share_*", "unshare_*", "mark_*", "prepare_*")),
    Vendor(
        name="github", title="GitHub",
        # The read-only address of GitHub's server: "/readonly - Default
        # toolset in read-only mode", where it offers no tool that writes.
        # https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md,
        # 2026-11-19.
        url="https://api.githubcopilot.com/mcp/readonly",
        # A GitHub App's sign-in takes no scope: what a token may do is the
        # app's own permissions, which are set to read-only at GitHub when
        # the app is registered (hosted/README.md, "GitHub").
        scope="",
        writes=(
            "create_*", "update_*", "delete_*", "remove_*", "add_*", "merge_*", "push_*",
            "fork_*", "*_write", "request_*", "assign_*", "actions_run_*", "run_*",
            "rerun_*", "cancel_*", "dismiss_*", "mark_*", "manage_*", "star_*",
            "unstar_*"),
        # https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app,
        # 2026-11-19.
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        # GitHub names itself with a path, and sends this back as `iss`. Read
        # 2026-11-20 from
        # https://github.com/.well-known/oauth-authorization-server/login/oauth.
        # The first real sign-in was refused while this was the bare origin.
        issuer="https://github.com/login/oauth",
        note="It reads the repositories the Waku app is installed on at GitHub."),
)}


def is_connector_name(name: object) -> bool:
    """Whether `name` is on the list. The one check every route, the gateway's
    socket and the spawner make before a name reaches a path or a file."""
    return isinstance(name, str) and name in VENDORS
