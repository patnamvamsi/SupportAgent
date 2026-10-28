"""Knowledge search (ADR-0011 §4, WP-M08): the read-only ``knowledge.search`` capability.

``domain.knowledge.KnowledgeIndex`` (BM25 over ingested chunks) implements it;
an MCP- or Confluence-backed source will implement the same call.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from supportagent.domain.knowledge import DocHit, DocScope


class KnowledgeSearch(Protocol):
    def search(self, query: str, scope: DocScope, *, k: int, today: date) -> list[DocHit]:
        """The best chunk per doc, in scope, best first; stale hits flagged."""
        ...


__all__ = ["KnowledgeSearch"]
