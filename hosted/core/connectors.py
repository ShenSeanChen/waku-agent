"""The connectors a person on hosted Waku can sign in to (spec 029).

Two vendors in v1, Notion and Linear. Each is the vendor's own hosted MCP
server, and each takes sign-in from a client that registers itself (dynamic
client registration). GitHub is left out: it takes a personal access token,
and a token typed into this platform's page is the spec's open question 3.

A person cannot add another address. The gateway calls a connector with the
platform's network position, so the list is closed and changes by pull
request.

THIS IS A SECOND COPY OF TWO ENTRIES OF waku/connectors.toml, the list the
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
    """
    name: str
    title: str
    url: str
    scope: str
    writes: tuple[str, ...]

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
)}


def is_connector_name(name: object) -> bool:
    """Whether `name` is on the list. The one check every route, the gateway's
    socket and the spawner make before a name reaches a path or a file."""
    return isinstance(name, str) and name in VENDORS
