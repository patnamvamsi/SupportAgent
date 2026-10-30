"""What onboarding ingest reads (ADR-0015 P1, WP-O2): the records adapters return.

Pure data. The Confluence exporter returns :class:`IngestedPage`, the
repository readers fill :class:`IngestedFiles`, and the ServiceNow exporter
returns flat string records. ``app.ingest`` redacts them and the workspace
stores them in the onboarding lab's layout, so every later stage reads a
real system and a reference system the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class IngestedPage:
    page_id: str
    title: str
    version: str | None
    last_updated: date | None
    labels: tuple[str, ...]
    url: str | None
    markdown: str


@dataclass(slots=True)
class IngestedFiles:
    #: ``<repo>/<path>`` -> text
    files: dict[str, str] = field(default_factory=dict)
    #: ``<repo>/<path>`` -> why it wasn't read (too large, binary, budget)
    skipped: dict[str, str] = field(default_factory=dict)
    repos: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return any(reason == "file budget reached" for reason in self.skipped.values())


@dataclass(slots=True)
class TicketHistory:
    """A ticketing system's history for one system, as flat string records.

    The connector reads it (scope, queries, field names are the vendor's);
    onboarding redacts the text fields and stores it. ``cmdb`` holds the
    configuration items (``cis``) and their ``relationships``.
    """

    incidents: list[dict[str, str]] = field(default_factory=list)
    problems: list[dict[str, object]] = field(default_factory=list)
    changes: list[dict[str, str]] = field(default_factory=list)
    cis: list[dict[str, str]] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
    #: The incident budget was reached: more exist than were read.
    truncated: bool = False


#: Free-text fields onboarding redacts in ticket records (INV-06).
TICKET_TEXT_FIELDS = ("short_description", "description", "close_notes")

__all__ = ["TICKET_TEXT_FIELDS", "IngestedFiles", "IngestedPage", "TicketHistory"]
