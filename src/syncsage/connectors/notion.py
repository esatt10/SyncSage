"""Notion connector (Product Framework Step 31.2) — first-party SDK plugin.

Indexes every page a Notion integration can see: pages are listed through
the paginated ``POST /v1/search`` endpoint and read by walking each page's
block tree (``GET /v1/blocks/{id}/children``) into deterministic Markdown —
rule-based rendering only, no LLM (pillar 3).

The four pillars, concretely:

- **Deterministic**: fixed block→Markdown rules, stable ordering, stable
  identities (``notion:<source>:<page_id>``); same API payloads → byte-
  identical artifacts.
- **Idempotent**: ``item.sha256`` is a version proxy over
  ``(page_id, last_edited_time)``, so the engine's pre-read skip drops
  unchanged pages before any block fetch.
- **Incremental**: the checkpoint cursor stores per-page
  ``last_edited_time``; on an incremental pass ``read_item`` raises
  :class:`ItemNotModified` for unchanged pages (no transfer), and the high
  watermark records the max edit time.
- **State discipline**: checkpoints go through the provided ``StateStore``
  only. The integration token is read from the environment variable named
  by ``sources[].connector.api_key_env`` (default ``NOTION_TOKEN``) — the
  secret never lands in config or state (Phase-12.3 convention).

ACL capture (reserved for Phase 32): each item carries
``metadata["acl"]`` with the page's ``created_by`` / ``last_edited_by``
principal ids — captured now, enforced when principal filtering lands.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from syncsage.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    ItemNotModified,
    SourceConnector,
)

DEFAULT_BASE_URL = "https://api.notion.com/v1"
DEFAULT_TOKEN_ENV = "NOTION_TOKEN"
NOTION_VERSION = "2022-06-28"
MAX_BLOCK_DEPTH = 3
PAGE_SIZE = 100

_TEXT_BLOCKS = {
    "paragraph": "{text}",
    "heading_1": "# {text}",
    "heading_2": "## {text}",
    "heading_3": "### {text}",
    "bulleted_list_item": "- {text}",
    "numbered_list_item": "1. {text}",
    "quote": "> {text}",
    "callout": "> {text}",
    "toggle": "{text}",
}


def _notion_request(
    url: str, token: str, timeout: int, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One Notion API call (POST when ``payload`` given, else GET).

    Module-level so tests monkeypatch it with fixture-backed fakes — the
    only network touchpoint in this connector.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "User-Agent": "SyncSage/0.3",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, headers=headers, data=data)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:  # pragma: no cover - exercised via fakes
        raise ConnectorUnavailable(f"Notion API {exc.code} for {url}: {exc.reason}") from exc


def _plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join(str(part.get("plain_text") or "") for part in rich_text or [])


def _page_title(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            title = _plain_text(prop.get("title") or [])
            if title:
                return title
    return "untitled"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "untitled"


class NotionConnector(SourceConnector):
    connector_type = "notion"

    # ------------------------------------------------------------- plumbing
    def _token(self) -> str:
        import os

        env_name = self.source.connector.api_key_env or DEFAULT_TOKEN_ENV
        token = os.environ.get(env_name)
        if not token:
            raise ConnectorUnavailable(
                f"notion connector for source {self.source.name} needs an "
                f"integration token in ${env_name} (create one at "
                "notion.so/my-integrations and share the pages with it)"
            )
        return token

    def _base_url(self) -> str:
        return (self.source.connector.api_endpoint or DEFAULT_BASE_URL).rstrip("/")

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return _notion_request(
            f"{self._base_url()}{path}",
            self._token(),
            self.source.connector.request_timeout_seconds,
            payload,
        )

    # ------------------------------------------------------------- listing
    def list_items(self) -> list[ConnectorItem]:
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"property": "object", "value": "page"},
                "page_size": PAGE_SIZE,
            }
            if cursor:
                body["start_cursor"] = cursor
            result = self._request("/search", body)
            pages.extend(r for r in result.get("results", []) if r.get("object") == "page")
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

        items: list[ConnectorItem] = []
        for page in sorted(pages, key=lambda p: str(p.get("id"))):
            page_id = str(page.get("id"))
            edited = str(page.get("last_edited_time") or "")
            title = _page_title(page)
            relative = f"{_slug(title)}-{page_id.replace('-', '')[:8]}.md"
            if not self._allows_relative_path(relative):
                continue
            items.append(
                ConnectorItem(
                    identity=f"notion:{self.source.name}:{page_id}",
                    relative_path=relative,
                    uri=str(page.get("url") or f"notion://{page_id}"),
                    mime_type="text/markdown",
                    # Version proxy: unchanged (page, edit-time) → engine's
                    # pre-read manifest skip fires before any block fetch.
                    sha256=hashlib.sha256(f"{page_id}:{edited}".encode()).hexdigest(),
                    mtime=edited or None,
                    etag=edited or None,
                    metadata={
                        "page_id": page_id,
                        "title": title,
                        "last_edited_time": edited,
                        # Phase 32 ACL capture (reserved): principal ids only.
                        "acl": {
                            "created_by": (page.get("created_by") or {}).get("id"),
                            "last_edited_by": (page.get("last_edited_by") or {}).get("id"),
                        },
                    },
                )
            )
        return items

    # ------------------------------------------------------------- reading
    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        page_id = str(item.metadata["page_id"])
        if self.sync_mode == "incremental" and self._previous_cursor:
            previous_edit = (self._previous_cursor.get("page_edits") or {}).get(page_id)
            if previous_edit and previous_edit == item.metadata.get("last_edited_time"):
                raise ItemNotModified(f"notion page {page_id} unchanged since {previous_edit}")
        lines = [f"# {item.metadata.get('title', 'untitled')}", ""]
        lines.extend(self._render_blocks(page_id, depth=0))
        content = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type="text/markdown",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata=dict(item.metadata),
        )

    def _render_blocks(self, block_id: str, depth: int) -> list[str]:
        if depth >= MAX_BLOCK_DEPTH:
            return []
        lines: list[str] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{block_id}/children?page_size={PAGE_SIZE}"
            if cursor:
                path += f"&start_cursor={cursor}"
            result = self._request(path)
            for block in result.get("results", []):
                lines.extend(self._render_block(block, depth))
            if not result.get("has_more"):
                return lines
            cursor = result.get("next_cursor")

    def _render_block(self, block: dict[str, Any], depth: int) -> list[str]:
        block_type = str(block.get("type") or "")
        payload = block.get(block_type) or {}
        indent = "  " * depth
        lines: list[str] = []
        if block_type in _TEXT_BLOCKS:
            text = _plain_text(payload.get("rich_text") or [])
            if text:
                lines.append(indent + _TEXT_BLOCKS[block_type].format(text=text))
        elif block_type == "to_do":
            text = _plain_text(payload.get("rich_text") or [])
            mark = "x" if payload.get("checked") else " "
            if text:
                lines.append(f"{indent}- [{mark}] {text}")
        elif block_type == "code":
            text = _plain_text(payload.get("rich_text") or [])
            language = str(payload.get("language") or "")
            lines.extend([f"{indent}```{language}", indent + text, f"{indent}```"])
        elif block_type == "divider":
            lines.append(f"{indent}---")
        if block.get("has_children"):
            lines.extend(self._render_blocks(str(block.get("id")), depth + 1))
        return lines

    # ---------------------------------------------------------- checkpoint
    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        page_edits = {
            str(item.metadata["page_id"]): str(item.metadata.get("last_edited_time") or "")
            for item in items
        }
        newest = max(page_edits.values(), default="")
        return {"page_edits": page_edits}, {"max_last_edited": newest, "page_count": len(items)}
