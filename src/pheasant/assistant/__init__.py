"""Grounded chat over the knowledge graph (query-time, never indexing).

``providers`` is the multi-vendor chat client, ``credentials`` the
process-local session-key vault, ``chat`` the retrieval → grounding →
answer pipeline. Import lazily from the API layer so a deployment that
never opens the chat surface pays nothing.
"""

from pheasant.assistant.chat import answer_question
from pheasant.assistant.credentials import SessionCredential, SessionKeyStore
from pheasant.assistant.providers import PROVIDERS, ProviderError, ProviderSpec, complete

__all__ = [
    "PROVIDERS",
    "ProviderError",
    "ProviderSpec",
    "SessionCredential",
    "SessionKeyStore",
    "answer_question",
    "complete",
]
