"""Episodic-memory adapter backed by a Notion database.

Each episode becomes a page in a database with two properties:
- Name (title): the ISO-8601 timestamp string (happened_at)
- Summary (rich_text): the episode summary

Install with the [notion] extra (notion-client >= 2.5, data-sources API):
    pip install 'milli-agent[notion]'

Set environment variables:
    NOTION_TOKEN=<integration token>
    NOTION_EPISODES_DATABASE_ID=<database id>
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse


def normalize_database_id(value: str) -> str:
    """Accept a Notion database ID or a copied database URL.

    Notion puts the database object ID in the URL path and a view ID in the
    ``v`` query parameter. Keeping this conversion here lets every entry point
    accept the link users naturally copy from Notion.
    """
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        match = re.search(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", parsed.path.lower())
        if not match:
            raise ValueError("Notion database link must include a database ID in its path")
        return match.group(0)
    return value


class NotionEpisodeStore:
    def __init__(self, token: str | None = None, database_id: str | None = None) -> None:
        from notion_client import Client

        self.token = token or os.environ.get("NOTION_TOKEN")
        raw_database_id = database_id or os.environ.get("NOTION_EPISODES_DATABASE_ID", "")
        self.database_id = normalize_database_id(raw_database_id) if raw_database_id else ""
        if not self.token:
            raise ValueError(
                "Notion token required: pass token= or set NOTION_TOKEN environment variable"
            )
        if not self.database_id:
            raise ValueError(
                "Notion database_id required: pass database_id= or set "
                "NOTION_EPISODES_DATABASE_ID environment variable"
            )
        self.client = Client(auth=self.token)
        # notion-client >= 2.5 (Notion API 2025-09-03): rows live under a
        # database's data source, so resolve database_id -> data_source_id once.
        sources = self.client.databases.retrieve(database_id=self.database_id).get(
            "data_sources"
        ) or []
        if not sources:
            raise ValueError(
                f"Notion database {self.database_id} has no data sources to query"
            )
        self.data_source_id = sources[0]["id"]

    def add(self, summary: str, happened_at: str) -> None:
        """Create a new episode page in the configured Notion database."""
        self.client.pages.create(
            parent={"data_source_id": self.data_source_id},
            properties={
                "Name": {"title": [{"text": {"content": happened_at}}]},
                "Summary": {"rich_text": [{"text": {"content": summary}}]},
            },
        )

    def recent(self, top_k: int = 3) -> list[str]:
        """Return the latest N episodes, most recent happened_at first."""
        pages = self._query_all()
        pages.sort(
            key=lambda p: self._extract_title(p.get("properties", {}).get("Name", {})),
            reverse=True,
        )
        return [self._format(p) for p in pages[:top_k]]

    def search(self, query: str, top_k: int = 3) -> list[str]:
        """Keyword search over episode summaries; falls back to recent if query is empty."""
        keywords = re.findall(r"[a-zA-Z0-9]{2,}", query.lower())
        if not keywords:
            return self.recent(top_k)

        # Deduplicate while preserving order.
        keywords = list(dict.fromkeys(keywords))
        matches = []
        for page in self._query_all():
            summary_lower = self._extract_rich_text(
                page.get("properties", {}).get("Summary", {})
            ).lower()
            if any(
                re.search(rf"\b{re.escape(keyword)}\b", summary_lower)
                for keyword in keywords
            ):
                matches.append(page)

        matches.sort(
            key=lambda p: self._extract_title(p.get("properties", {}).get("Name", {})),
            reverse=True,
        )
        return [self._format(p) for p in matches[:top_k]]

    def list(self, limit: int = 200) -> list[dict]:
        """Return all episodes as dicts, sorted by happened_at descending."""
        pages = self._query_all()
        pages.sort(
            key=lambda p: self._extract_title(p.get("properties", {}).get("Name", {})),
            reverse=True,
        )
        return [
            {
                "id": page.get("id", ""),
                "happened_at": self._extract_title(
                    page.get("properties", {}).get("Name", {})
                ),
                "summary": self._extract_rich_text(
                    page.get("properties", {}).get("Summary", {})
                ),
                "created_at": page.get("created_time", ""),
            }
            for page in pages[:limit]
        ]

    def delete(self, episode_id: int | str) -> bool:
        """Archive the Notion page for the given episode id."""
        self.client.pages.update(page_id=str(episode_id), archived=True)
        return True

    def _query_all(self) -> list[dict]:
        """Fetch all pages from the database, following pagination."""
        results: list[dict] = []
        response = self.client.data_sources.query(data_source_id=self.data_source_id)
        results.extend(response.get("results", []))
        while response.get("has_more"):
            response = self.client.data_sources.query(
                data_source_id=self.data_source_id,
                start_cursor=response.get("next_cursor"),
            )
            results.extend(response.get("results", []))
        return results

    def _format(self, page: dict) -> str:
        props = page.get("properties", {})
        happened_at = self._extract_title(props.get("Name", {}))
        summary = self._extract_rich_text(props.get("Summary", {}))
        return f"({happened_at}) {summary}"

    @staticmethod
    def _extract_title(prop: dict) -> str:
        return "".join(part.get("text", {}).get("content", "") for part in prop.get("title", []))

    @staticmethod
    def _extract_rich_text(prop: dict) -> str:
        return "".join(
            part.get("text", {}).get("content", "") for part in prop.get("rich_text", [])
        )
