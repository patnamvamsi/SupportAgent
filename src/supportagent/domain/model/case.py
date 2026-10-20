"""A triage case (the unit the classifier decides on, 01 §4)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from supportagent.domain.model.base import DomainModel
from supportagent.domain.severity import Severity


class Case(DomainModel):
    """The case a classifier decision is made about (01 §4).

    ``service`` is the catalog-resolved service name (``None`` when the catalog
    could not resolve it — the classifier then safe-defaults, ARCH §4). The
    full case row lives in the persistence layer; this is the domain projection
    the classifier and router need. ``external_table``/``external_id`` are the
    source-system coordinates the work-note scope and target reference (10-wp
    WP-011); ``severity``/``classification`` are the case's current ranked
    severity and classifier label (``None`` until decided).
    """

    id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    origin: Literal["alert", "ticket"]
    service: str | None = None
    external_table: str | None = None
    external_id: str | None = None
    severity: Severity | None = None
    classification: str | None = None
