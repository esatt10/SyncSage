"""Refusals the transports translate, rather than a transport's own errors.

An operation cannot raise `HTTPException` — that would put FastAPI inside the
layer both an HTTP server and an MCP server call. It cannot raise the MCP
SDK's `ToolError` either, for the mirror-image reason. So it raises these, and
each adapter maps them to its own vocabulary: a status code on one side, a
`ToolError` at the SDK boundary on the other.

Every one subclasses ``ValueError``. That is deliberate and is what makes this
change safe to land incrementally: `mcp_server/server.py` already translates
`ValueError`/`KeyError` into an informative `ToolError`, and the HTTP surface
already turns them into a 400. A service refusal therefore behaves correctly
on both surfaces from the first commit, and the adapters can be taught the
richer mapping one at a time rather than all at once.

The **message is part of the contract**. "Unknown source: notes" is what an
agent reads when it mistypes a name, and an agent told only that something
failed will retry the same call. The conformance test asserts both surfaces
produce the same text, because two spellings of one refusal is the same defect
as two implementations of one operation, one level down.
"""

from __future__ import annotations


class ServiceError(ValueError):
    """Base for every deliberate refusal an operation makes.

    ``status`` is a hint for an HTTP adapter, not a coupling: nothing in the
    layer imports a web framework, and an adapter is free to ignore it.
    """

    status: int = 400


class UnknownKnowledgeBase(ServiceError):
    status = 404

    def __init__(self, requested: str) -> None:
        super().__init__(f"Unknown knowledge base: {requested}")
        self.requested = requested


class SourceNotFound(ServiceError):
    status = 404

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown source: {name}")
        self.name = name


class NodeNotFound(ServiceError):
    status = 404

    def __init__(self, node_id: str) -> None:
        super().__init__(f"Unknown node: {node_id}")
        self.node_id = node_id


class NotPermitted(ServiceError):
    """The caller may not read this artifact.

    Deliberately says nothing about whether it exists: an ACL that
    distinguishes "forbidden" from "absent" is an enumeration oracle.
    """

    status = 403

    def __init__(self, detail: str = "Not permitted") -> None:
        super().__init__(detail)


class InvalidRequest(ServiceError):
    """A request the operation cannot make sense of."""

    status = 422
