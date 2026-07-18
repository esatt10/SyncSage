"""Slack connector (Product Framework Step 31.4) — SDK plugin.

One artifact per channel: the channel's recent history rendered as a
deterministic Markdown transcript (timestamp-ordered, ``**<user>**:``
lines, thread replies indented). Listing via ``conversations.list``
(cursor-paginated), history via ``conversations.history``. Incremental:
``item.sha256`` is a ``(channel_id, latest_ts)`` version proxy — a channel
with no new messages is skipped by the engine before any history fetch,
and the per-channel cursor lets ``read_item`` raise
:class:`ItemNotModified`. Bot token from ``connector.api_key_env``
(default ``SLACK_TOKEN``). ACL capture (Phase 32 reserved):
``metadata["acl"]`` carries ``is_private`` + ``is_shared``. User ids are
rendered as-is (no ``users.list`` join) to keep reads deterministic and
single-endpoint; map ids to names downstream if needed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from syncsage.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    ItemNotModified,
    SourceConnector,
)

DEFAULT_BASE_URL = "https://slack.com/api"
DEFAULT_TOKEN_ENV = "SLACK_TOKEN"
PAGE_SIZE = 200
HISTORY_LIMIT = 500


def _slack_request(url: str, token: str, timeout: int) -> dict[str, Any]:
    """One Slack Web API GET; module-level so tests monkeypatch it."""
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:  # pragma: no cover - exercised via fakes
        raise ConnectorUnavailable(f"Slack API {exc.code} for {url}: {exc.reason}") from exc
    if not payload.get("ok", False):
        raise ConnectorUnavailable(f"Slack API error for {url}: {payload.get('error')}")
    return payload


class SlackConnector(SourceConnector):
    connector_type = "slack"

    def _token(self) -> str:
        import os

        env_name = self.source.connector.api_key_env or DEFAULT_TOKEN_ENV
        token = os.environ.get(env_name)
        if not token:
            raise ConnectorUnavailable(
                f"slack connector for source {self.source.name} needs a bot "
                f"token in ${env_name} (scopes: channels:history, channels:read)"
            )
        return token

    def _base_url(self) -> str:
        return (self.source.connector.api_endpoint or DEFAULT_BASE_URL).rstrip("/")

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        url = f"{self._base_url()}/{method}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        return _slack_request(url, self._token(), self.source.connector.request_timeout_seconds)

    def list_items(self) -> list[ConnectorItem]:
        channels: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = self._call("conversations.list", limit=PAGE_SIZE, cursor=cursor or None)
            channels.extend(result.get("channels", []))
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

        items: list[ConnectorItem] = []
        for channel in sorted(channels, key=lambda c: str(c.get("id"))):
            if channel.get("is_archived"):
                continue
            channel_id = str(channel.get("id"))
            name = str(channel.get("name") or channel_id)
            latest = str((channel.get("latest") or {}).get("ts") or channel.get("updated") or "")
            relative = f"{name}-{channel_id[:8]}.md".lower()
            if not self._allows_relative_path(relative):
                continue
            items.append(
                ConnectorItem(
                    identity=f"slack:{self.source.name}:{channel_id}",
                    relative_path=relative,
                    uri=f"slack://channel/{channel_id}",
                    mime_type="text/markdown",
                    sha256=hashlib.sha256(f"{channel_id}:{latest}".encode()).hexdigest(),
                    mtime=latest or None,
                    etag=latest or None,
                    metadata={
                        "channel_id": channel_id,
                        "channel_name": name,
                        "latest_ts": latest,
                        # Phase 32 ACL capture (reserved).
                        "acl": {
                            "is_private": bool(channel.get("is_private")),
                            "is_shared": bool(channel.get("is_shared")),
                        },
                    },
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        channel_id = str(item.metadata["channel_id"])
        if self.sync_mode == "incremental" and self._previous_cursor:
            previous = (self._previous_cursor.get("channel_latest") or {}).get(channel_id)
            if previous and previous == item.metadata.get("latest_ts"):
                raise ItemNotModified(f"slack channel {channel_id} unchanged at ts {previous}")

        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(messages) < HISTORY_LIMIT:
            result = self._call(
                "conversations.history",
                channel=channel_id,
                limit=min(PAGE_SIZE, HISTORY_LIMIT - len(messages)),
                cursor=cursor or None,
            )
            messages.extend(result.get("messages", []))
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
            if not result.get("has_more") or not cursor:
                break

        lines = [f"# #{item.metadata.get('channel_name', channel_id)}", ""]
        for message in sorted(messages, key=lambda m: float(m.get("ts") or 0.0)):
            if message.get("subtype"):  # joins, topic changes, bots-config noise
                continue
            user = str(message.get("user") or message.get("bot_id") or "unknown")
            text = str(message.get("text") or "").strip()
            if text:
                lines.append(f"**{user}** ({message.get('ts')}): {text}")
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

    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        latest = {
            str(item.metadata["channel_id"]): str(item.metadata.get("latest_ts") or "")
            for item in items
        }
        newest = max(latest.values(), default="")
        return {"channel_latest": latest}, {"max_ts": newest, "channel_count": len(items)}
