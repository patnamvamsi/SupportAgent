"""External system references (01 §4, §7.5).

An ``ExternalRef`` identifies a resource in an external system (ServiceNow,
Airflow, ...). The executor stores ``as_text()`` in the outbox's
``external_ref`` column so a reconciled send can be traced back to the exact
external write it corresponds to (ARCH §6.2, INV-04).
"""

from __future__ import annotations

from supportagent.domain.model.base import DomainModel


class ExternalRef(DomainModel):
    """A reference to one resource in one external system (01 §7.5).

    ``system`` is the coarse system name (``"servicenow"``) and ``ref`` is the
    system's own identifier for the resource (table/row or field).
    """

    system: str
    ref: str

    def as_text(self) -> str:
        """The compact ``system:ref`` form stored on the outbox row."""
        return f"{self.system}:{self.ref}"


class ExternalCaseRef(DomainModel):
    """A reference to one case (record) in an external system (01 §7.5).

    Identifies the record a set of ``ChangeRecord``s apply to: the vendor
    table, the record's ``sys_id``, and (when present) its human number.
    ``InboundHandler.on_changes`` receives it alongside the changes (02 §6).
    """

    table: str
    sys_id: str
    number: str | None = None


__all__ = ["ExternalCaseRef", "ExternalRef"]
