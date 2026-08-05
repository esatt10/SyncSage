"""Confluence connector (Product Framework Step 31.5) — SDK plugin.

Indexes the pages a Confluence API token can see: listing via the
``rest/api/content`` endpoint (``start``/``limit`` pagination, storage
body + version + space expanded in one call — content arrives with the
listing, so reads are network-free), content = the page's XHTML storage
body reduced to deterministic text via BeautifulSoup (a core dependency;
rule-based, no LLM). Incremental: ``item.sha256`` is a ``(page_id,
version_number)`` proxy and the per-page cursor lets ``read_item`` raise
:class:`ItemNotModified`. Token from ``connector.api_key_env`` (default
``CONFLUENCE_TOKEN``); the site base URL comes from
``connector.api_endpoint`` (required — every Confluence site has its own
host). ACL capture (Phase 32 reserved): ``metadata["acl"]`` carries the
space key + the page creator's account id.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from pheasant.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    ItemNotModified,
    SourceConnector,
)

DEFAULT_TOKEN_ENV = "CONFLUENCE_TOKEN"
PAGE_SIZE = 50
EXPAND = "body.storage,version,space,history"


def _confluence_request(url: str, token: str, timeout: int) -> dict[str, Any]:
    """One Confluence REST GET; module-level so tests monkeypatch it."""
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:  # pragma: no cover - exercised via fakes
        raise ConnectorUnavailable(f"Confluence API {exc.code} for {url}: {exc.reason}") from exc


def _storage_to_text(xhtml: str) -> str:
    """Deterministic text projection of Confluence storage-format XHTML."""
    soup = BeautifulSoup(xhtml or "", "html.parser")
    blocks: list[str] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "pre"]):
        text = element.get_text(" ", strip=True)
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4"}:
            blocks.append("#" * int(element.name[1]) + " " + text)
        elif element.name == "li":
            blocks.append(f"- {text}")
        elif element.name == "pre":
            blocks.extend(["```", text, "```"])
        else:
            blocks.append(text)
    if blocks:
        return "\n\n".join(blocks)
    return soup.get_text(" ", strip=True)


class ConfluenceConnector(SourceConnector):
    connector_type = "confluence"

    def _token(self) -> str:
        import os

        env_name = self.source.connector.api_key_env or DEFAULT_TOKEN_ENV
        token = os.environ.get(env_name)
        if not token:
            raise ConnectorUnavailable(
                f"confluence connector for source {self.source.name} needs an "
                f"API token in ${env_name}"
            )
        return token

    def _base_url(self) -> str:
        base = self.source.connector.api_endpoint
        if not base:
            raise ConnectorUnavailable(
                f"confluence connector for source {self.source.name} needs "
                "connector.api_endpoint (e.g. https://your-site.atlassian.net/wiki)"
            )
        return base.rstrip("/")

    def _pages(self) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        start = 0
        while True:
            params = {"type": "page", "start": start, "limit": PAGE_SIZE, "expand": EXPAND}
            result = _confluence_request(
                f"{self._base_url()}/rest/api/content?{urlencode(params)}",
                self._token(),
                self.source.connector.request_timeout_seconds,
            )
            batch = result.get("results", [])
            pages.extend(batch)
            if len(batch) < PAGE_SIZE or result.get("size", 0) < PAGE_SIZE:
                break
            start += PAGE_SIZE
        return pages

    def list_items(self) -> list[ConnectorItem]:
        items: list[ConnectorItem] = []
        for page in sorted(self._pages(), key=lambda p: str(p.get("id"))):
            page_id = str(page.get("id"))
            title = str(page.get("title") or "untitled")
            version = int((page.get("version") or {}).get("number") or 0)
            space = str((page.get("space") or {}).get("key") or "")
            relative = f"{space.lower() or 'space'}/{page_id}.md"
            if not self._allows_relative_path(relative):
                continue
            body = str(((page.get("body") or {}).get("storage") or {}).get("value") or "")
            creator = ((page.get("history") or {}).get("createdBy") or {}).get("accountId")
            items.append(
                ConnectorItem(
                    identity=f"confluence:{self.source.name}:{page_id}",
                    relative_path=relative,
                    uri=f"{self._base_url()}/pages/{page_id}",
                    mime_type="text/markdown",
                    sha256=hashlib.sha256(f"{page_id}:v{version}".encode()).hexdigest(),
                    mtime=str((page.get("version") or {}).get("when") or "") or None,
                    etag=f"v{version}",
                    metadata={
                        "page_id": page_id,
                        "title": title,
                        "version": version,
                        "storage_body": body,  # arrives with the listing
                        # Phase 32 ACL capture (reserved).
                        "acl": {"space": space, "created_by": creator},
                    },
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        page_id = str(item.metadata["page_id"])
        if self.sync_mode == "incremental" and self._previous_cursor:
            previous = (self._previous_cursor.get("page_versions") or {}).get(page_id)
            if previous is not None and int(previous) == int(item.metadata.get("version") or 0):
                raise ItemNotModified(f"confluence page {page_id} unchanged at v{previous}")
        text = _storage_to_text(str(item.metadata.get("storage_body") or ""))
        content = (f"# {item.metadata.get('title', 'untitled')}\n\n{text}".rstrip() + "\n").encode(
            "utf-8"
        )
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type="text/markdown",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata={k: v for k, v in item.metadata.items() if k != "storage_body"},
        )

    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        versions = {
            str(item.metadata["page_id"]): int(item.metadata.get("version") or 0) for item in items
        }
        newest = max(versions.values(), default=0)
        return {"page_versions": versions}, {"max_version": newest, "page_count": len(items)}
