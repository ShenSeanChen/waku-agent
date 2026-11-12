"""Where plugins come from (spec 021 B1).

A marketplace is a repository with `.claude-plugin/marketplace.json`, the
file Claude Code reads:

    {"name": "waku-memory",
     "plugins": [{"name": "waku", "source": "./plugins/waku"}]}

Waku records each one in <home>/plugins/marketplaces.json under that name:

    {"waku-memory": {"repo": "waku-one/waku-memory-plugins", "ref": "main"}}

A marketplace on GitHub is fetched as one archive of the repository at its
ref, over HTTPS with the standard library: no `git`, no new dependency. A
folder on this machine (`{"path": ...}`) is read where it is, which is how a
plugin author tries a plugin before publishing it, and how the evals run
offline. Waku installs a plugin whose `source` is a path inside the
repository, and no other kind.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import tarfile
import tempfile
import urllib.request
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from waku.plugins import store

FILE = "marketplaces.json"
CATALOG = Path(".claude-plugin") / "marketplace.json"
ARCHIVE_URL = "https://codeload.github.com/{repo}/tar.gz/{ref}"

# What one marketplace archive may cost (spec 021 review). A plugin is a few
# JSON files and some Markdown; these are far above any real one and far
# below what fills a disk.
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_EXTRACTED = 200 * 1024 * 1024
MAX_MEMBERS = 10_000

# The marketplace the first-run question installs Waku Memory from. Named
# here so `waku plugin install waku@waku-memory` works before any `add`.
KNOWN = {"waku-memory": {"repo": "waku-one/waku-memory-plugins", "ref": "main"}}

_REPO = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?(?:@([\w./-]+))?$")


class MarketplaceError(Exception):
    """Something the person can fix: a name, a source or a download."""


def read(home: Path) -> dict:
    try:
        data = json.loads((store.folder(home) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(home: Path, entries: dict) -> None:
    path = store.folder(home) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def entry_for(source: str) -> dict:
    """What `waku plugin marketplace add <source>` records: a folder on this
    machine, or a GitHub repository written owner/repo, optionally @ref."""
    folder = Path(source).expanduser()
    if folder.is_dir():
        return {"path": str(folder.resolve())}
    match = _REPO.match(source.strip())
    if not match:
        raise MarketplaceError(f"'{source}' is neither a folder nor a GitHub repository (owner/repo)")
    return {"repo": match.group(1), "ref": match.group(2) or "main"}


def lookup(home: Path, name: str) -> dict | None:
    """A built-in marketplace always wins its name, so a catalog that calls
    itself waku-memory cannot stand in for the official one."""
    return KNOWN.get(name) or read(home).get(name)


def same_source(a: dict, b: dict) -> bool:
    return (a.get("repo"), a.get("path")) == (b.get("repo"), b.get("path"))


def claim(home: Path, name: str, entry: dict) -> str | None:
    """Why `name` cannot be recorded for `entry`, or None when it can. A name
    that is built in, or recorded already for another source, stays with what
    it names now."""
    if not store.valid_name(name):
        return f"'{name}' is not a marketplace name Waku records."
    if name in KNOWN and not same_source(KNOWN[name], entry):
        return (f"'{name}' is the name of Waku's built-in marketplace "
                f"({KNOWN[name].get('repo')}), so another source cannot use it.")
    recorded = read(home).get(name)
    if recorded is not None and not same_source(recorded, entry):
        return (f"A marketplace named '{name}' is already recorded from "
                f"{recorded.get('repo') or recorded.get('path')}. Remove it first.")
    return None


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise MarketplaceError(f"the archive at {url} is larger than {MAX_DOWNLOAD // 2**20} MB")
    return data


def _extract(data: bytes, dest: Path) -> Path:
    """Unpack a GitHub archive into `dest` and return its one top folder.
    Only plain files and folders are written, and only inside `dest`: a link,
    a device or a `../` path in somebody's archive is skipped. GitHub puts
    the archive's commit in the pax header's `comment`, and it is written to
    `<top>/.waku-commit` for the preview."""
    try:
        return _unpack(data, dest)
    except (tarfile.TarError, zlib.error, EOFError) as exc:
        # a proxy's HTML error page, a cut-off download, a corrupt archive
        raise MarketplaceError(f"the download is not a readable archive ({type(exc).__name__})") from exc


def _unpack(data: bytes, dest: Path) -> Path:
    root = dest.resolve()
    written = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        commit = str((tar.pax_headers or {}).get("comment", ""))
        members = []
        for member in tar:
            members.append(member)
            if len(members) > MAX_MEMBERS:
                raise MarketplaceError(f"the archive holds more than {MAX_MEMBERS} files")
        for member in members:
            target = (dest / member.name).resolve()
            if root not in target.parents:
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                written += member.size
                if written > MAX_EXTRACTED:
                    raise MarketplaceError(f"the archive unpacks to more than {MAX_EXTRACTED // 2**20} MB")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is not None:
                    target.write_bytes(source.read())
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise MarketplaceError("the downloaded archive is not one repository")
    if re.fullmatch(r"[0-9a-f]{7,40}", commit):
        (tops[0] / COMMIT_FILE).write_text(commit, encoding="utf-8")
    return tops[0]


COMMIT_FILE = ".waku-commit"


def resolved(root: Path, entry: dict) -> str:
    """What the preview names as the version it shows: the commit GitHub
    served, the ref when no commit came with it, or `local` for a folder."""
    if entry.get("path"):
        return "local"
    try:
        return (root / COMMIT_FILE).read_text(encoding="utf-8").strip()[:12]
    except OSError:
        return str(entry.get("ref") or "main")


@contextmanager
def fetch(entry: dict, ref: str | None = None) -> Iterator[Path]:
    """The marketplace's files, for the length of the `with`."""
    if entry.get("path"):
        folder = Path(entry["path"])
        if not folder.is_dir():
            raise MarketplaceError(f"{folder} is not a folder any more")
        yield folder
        return
    url = ARCHIVE_URL.format(repo=entry["repo"], ref=ref or entry.get("ref") or "main")
    try:
        data = _download(url)
    except (OSError, http.client.HTTPException) as exc:
        raise MarketplaceError(f"could not download {url}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="waku-plugin-") as tmp:
        yield _extract(data, Path(tmp))


def catalog(root: Path) -> dict:
    """The marketplace's own file: its name and its plugins."""
    try:
        data = json.loads((root / CATALOG).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MarketplaceError(f"no readable {CATALOG} in this marketplace") from exc
    if not isinstance(data, dict) or not data.get("name"):
        raise MarketplaceError(f"{CATALOG} names no marketplace")
    return data


def plugin_folder(root: Path, catalog_data: dict, name: str) -> Path:
    """The folder of plugin `name` inside the marketplace."""
    listed = next((p for p in catalog_data.get("plugins") or []
                   if isinstance(p, dict) and p.get("name") == name), None)
    if listed is None:
        raise MarketplaceError(f"marketplace '{catalog_data['name']}' has no plugin '{name}'")
    source = listed.get("source")
    if not isinstance(source, str):
        raise MarketplaceError(f"'{name}' is not a folder in the marketplace, so Waku cannot install it")
    folder = (root / source).resolve()
    if root.resolve() not in folder.parents or not folder.is_dir():
        raise MarketplaceError(f"'{name}' points outside the marketplace, so Waku will not install it")
    return folder
