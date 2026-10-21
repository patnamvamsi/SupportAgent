"""Paging (01 §3, ARCH §14; 11-wp WP-112): attach to an existing page, never create one.

``integration`` is the catalog's ``paging_integration`` value
(``pagerduty:<service id>``, ``opsgenie:<team>``). ``source_ref`` is the
dedup key the customer's monitoring used (the alert's source id). There is
no method that creates a page, picks recipients or changes routing: the
port can't express it (ARCH §14, a non-learnable surface).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from supportagent.domain.model.external import ExternalRef


@dataclass(frozen=True, slots=True)
class PageRef:
    provider: str  # "pagerduty" | "opsgenie"
    page_id: str
    url: str | None = None


class PagingIntegration(Protocol):
    def supports(self, integration: str) -> bool:
        """True when a real provider answers for ``integration`` (else the Null Object)."""
        ...

    def find_open_page(self, integration: str, source_ref: str) -> PageRef | None:
        """The open page for this service and dedup key; raises ``ExternalServiceError``."""
        ...

    def find_note(self, page: PageRef, marker: str) -> ExternalRef | None:
        """A note on ``page`` whose text contains ``marker`` (INV-04 reconciliation)."""
        ...

    def add_note(self, page: PageRef, text: str) -> ExternalRef:
        """Append a note to ``page``; raises ``ExternalServiceError``."""
        ...


__all__ = ["PageRef", "PagingIntegration"]
