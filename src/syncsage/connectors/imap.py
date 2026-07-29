"""IMAP email connector (Product Framework Step 31.6) — SDK plugin.

Indexes one mailbox (the source's ``path``, default ``INBOX``) over
IMAP4/SSL using only the standard library (``imaplib`` + ``email``).
Messages are immutable, which makes incremental sync exact: the checkpoint
cursor stores the highest indexed UID and an incremental pass lists only
``UID > last_uid`` (already-indexed artifacts stay untouched — incremental
never prunes). ``item.sha256`` derives from the immutable
``(uid, Message-ID)`` pair. Rendering is deterministic: headers + the
first ``text/plain`` part, CRLF-normalized. Credentials come from the env
var named by ``connector.api_key_env`` (default ``IMAP_CREDENTIALS``,
format ``user:password``); the server host[:port] from
``connector.api_endpoint``. ACL capture (Phase 32 reserved):
``metadata["acl"]`` carries the From/To/Cc addresses.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
from typing import Any

from syncsage.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    SourceConnector,
)

DEFAULT_TOKEN_ENV = "IMAP_CREDENTIALS"
HEADER_FIELDS = ("Message-ID", "Subject", "From", "To", "Cc", "Date")


def _imap_connect(host: str, port: int, timeout: int) -> imaplib.IMAP4:
    """Open the IMAP4/SSL session; module-level so tests inject a fake."""
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)  # pragma: no cover


class ImapConnector(SourceConnector):
    connector_type = "imap"

    # ------------------------------------------------------------- plumbing
    def _credentials(self) -> tuple[str, str]:
        import os

        env_name = self.source.connector.api_key_env or DEFAULT_TOKEN_ENV
        raw = os.environ.get(env_name)
        if not raw or ":" not in raw:
            raise ConnectorUnavailable(
                f"imap connector for source {self.source.name} needs "
                f"${env_name} set to user:password"
            )
        user, _, password = raw.partition(":")
        return user, password

    def _endpoint(self) -> tuple[str, int]:
        endpoint = self.source.connector.api_endpoint
        if not endpoint:
            raise ConnectorUnavailable(
                f"imap connector for source {self.source.name} needs "
                "connector.api_endpoint (e.g. imap.example.com or imap.example.com:993)"
            )
        host, _, port = endpoint.partition(":")
        return host, int(port or 993)

    def _mailbox(self) -> str:
        name = str(self.source.path).strip("/")
        return name.rsplit("/", 1)[-1] or "INBOX"

    def _session(self) -> imaplib.IMAP4:
        host, port = self._endpoint()
        user, password = self._credentials()
        client = _imap_connect(host, port, self.source.connector.request_timeout_seconds)
        client.login(user, password)
        client.select(self._mailbox(), readonly=True)
        return client

    @staticmethod
    def _uid_fetch(client: imaplib.IMAP4, uid: str, parts: str) -> bytes:
        status, data = client.uid("FETCH", uid, parts)
        if status != "OK" or not data:
            raise ConnectorUnavailable(f"IMAP FETCH failed for uid {uid}")
        for entry in data:
            if isinstance(entry, tuple) and len(entry) >= 2:
                return bytes(entry[1])
        return b""

    # ------------------------------------------------------------- listing
    def list_items(self) -> list[ConnectorItem]:
        last_uid = 0
        if self.sync_mode == "incremental" and self._previous_cursor:
            last_uid = int(self._previous_cursor.get("last_uid") or 0)
        client = self._session()
        try:
            criteria = f"UID {last_uid + 1}:*" if last_uid else "ALL"
            status, data = client.uid("SEARCH", None, criteria)
            if status != "OK":
                raise ConnectorUnavailable("IMAP SEARCH failed")
            uids = [u.decode() for u in (data[0] or b"").split() if int(u) > last_uid]
            items: list[ConnectorItem] = []
            fields = " ".join(HEADER_FIELDS)
            for uid in sorted(uids, key=int):
                raw_header = self._uid_fetch(client, uid, f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
                message = email.message_from_bytes(raw_header, policy=email.policy.default)
                message_id = str(message.get("Message-ID") or f"<uid-{uid}>").strip()
                subject = str(message.get("Subject") or "no subject")
                relative = f"{self._mailbox().lower()}/{uid}.md"
                if not self._allows_relative_path(relative):
                    continue
                items.append(
                    ConnectorItem(
                        identity=f"imap:{self.source.name}:{uid}",
                        relative_path=relative,
                        uri=f"imap://{self._mailbox()}/{uid}",
                        mime_type="text/markdown",
                        # Messages are immutable: uid + Message-ID is exact.
                        sha256=hashlib.sha256(f"{uid}:{message_id}".encode()).hexdigest(),
                        mtime=str(message.get("Date") or "") or None,
                        metadata={
                            "uid": uid,
                            "subject": subject,
                            "message_id": message_id,
                            # Phase 32 ACL capture (reserved).
                            "acl": {
                                "from": str(message.get("From") or ""),
                                "to": str(message.get("To") or ""),
                                "cc": str(message.get("Cc") or ""),
                            },
                        },
                    )
                )
            return items
        finally:
            client.logout()

    # ------------------------------------------------------------- reading
    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        client = self._session()
        try:
            raw = self._uid_fetch(client, str(item.metadata["uid"]), "(BODY.PEEK[])")
        finally:
            client.logout()
        message = email.message_from_bytes(raw, policy=email.policy.default)
        body = message.get_body(preferencelist=("plain",))
        text = str(body.get_content()) if body is not None else ""
        lines = [
            f"# {item.metadata.get('subject', 'no subject')}",
            "",
            f"From: {message.get('From', '')}",
            f"To: {message.get('To', '')}",
            f"Date: {message.get('Date', '')}",
            "",
            text.replace("\r\n", "\n").rstrip(),
        ]
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

    # ---------------------------------------------------------- checkpoint
    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        highest = max((int(item.metadata["uid"]) for item in items), default=0)
        if self._previous_cursor:
            highest = max(highest, int(self._previous_cursor.get("last_uid") or 0))
        return {"last_uid": highest}, {"last_uid": highest, "new_messages": len(items)}
