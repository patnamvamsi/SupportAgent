"""The paging Null Object and the provider router (ARCH §14, WP-112).

``NullPaging`` answers for an integration with no configured provider: it
supports nothing, finds no page, and refuses to add a note. ``RoutedPaging``
dispatches each ``paging_integration`` to the provider that supports it and
falls back to ``NullPaging``.
"""

from __future__ import annotations

from collections.abc import Sequence

from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.paging import PageRef, PagingIntegration


class NullPaging:
    def supports(self, integration: str) -> bool:
        return False

    def find_open_page(self, integration: str, source_ref: str) -> PageRef | None:
        return None

    def find_note(self, page: PageRef, marker: str) -> ExternalRef | None:
        return None

    def add_note(self, page: PageRef, text: str) -> ExternalRef:
        raise ExternalServiceError(f"no paging provider for {page.provider}")  # noqa: TRY003


class RoutedPaging:
    def __init__(self, providers: Sequence[PagingIntegration]) -> None:
        self._providers = list(providers)
        self._null = NullPaging()

    def _for(self, integration: str) -> PagingIntegration:
        return next((p for p in self._providers if p.supports(integration)), self._null)

    def supports(self, integration: str) -> bool:
        return self._for(integration) is not self._null

    def find_open_page(self, integration: str, source_ref: str) -> PageRef | None:
        return self._for(integration).find_open_page(integration, source_ref)

    def find_note(self, page: PageRef, marker: str) -> ExternalRef | None:
        return self._for(f"{page.provider}:").find_note(page, marker)

    def add_note(self, page: PageRef, text: str) -> ExternalRef:
        return self._for(f"{page.provider}:").add_note(page, text)


__all__ = ["NullPaging", "RoutedPaging"]
