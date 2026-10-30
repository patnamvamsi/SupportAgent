"""Mapping a system's incident history (ADR-0015 P3, WP-O4).

The ``map_incidents`` slot reads a batch of past incidents against the
catalogue (labels and causes) and names, for each, a label, a cause or none,
and a quote. Each reading is grounded (``domain.incident_map.ground``) and
``build_map`` forms the classes, letting problem records and change links
overrule the readings.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import ConfigDict, Field

from supportagent.domain.incident_map import (
    Catalogue,
    GroundedReading,
    IncidentMap,
    PastIncident,
    Reading,
    build_map,
    ground,
)
from supportagent.domain.model.base import DomainModel

#: Incidents per call: enough to share the catalogue, few enough to read each one.
BATCH = 8
_FIELD_MAX = 600


class ReadingAnswer(DomainModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    number: str
    label: str | None = None
    cause: str | None = None
    quote: str = ""


class IncidentsAnswer(DomainModel):
    """``map_incidents``'s JSON. Unknown keys are ignored; readings are grounded after."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    incidents: list[ReadingAnswer] = Field(default_factory=list)


#: ``read(labels_text, causes_text, incidents_text) -> answer or None``.
Reader = Callable[[str, str, str], IncidentsAnswer | None]


def incidents_text(batch: Sequence[PastIncident]) -> str:
    """The batch as the model sees it: one block per incident, long fields cut."""
    blocks = []
    for i in batch:
        lines = [
            f"### {i.number} (CI {i.ci}, {i.severity})",
            f"Title: {i.title[:_FIELD_MAX]}",
            f"Description: {i.description[:_FIELD_MAX]}",
            f"Resolution: {i.resolution[:_FIELD_MAX]}",
        ]
        if i.problem_title:
            lines.append(f"Problem record: {i.problem_title[:_FIELD_MAX]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def map_incidents(
    incidents: Sequence[PastIncident], cat: Catalogue, read: Reader | None
) -> IncidentMap:
    """The incident map: the readings (``read``, in batches), grounded, then grouped."""
    readings: dict[str, GroundedReading] = {}
    if read is not None:
        labels_text, causes_text = cat.text()
        by_number = {i.number: i for i in incidents}
        ordered = sorted(incidents, key=lambda i: i.number)
        for start in range(0, len(ordered), BATCH):
            batch = ordered[start : start + BATCH]
            answer = read(labels_text, causes_text, incidents_text(batch))
            if answer is None:
                continue
            wanted = {i.number for i in batch}
            for r in answer.incidents:
                if r.number in wanted and r.number not in readings:
                    reading = Reading(r.number, r.label, r.cause, r.quote)
                    readings[r.number] = ground(reading, by_number[r.number], cat)
    return build_map(incidents, readings)


__all__ = ["BATCH", "IncidentsAnswer", "Reader", "ReadingAnswer", "incidents_text", "map_incidents"]
