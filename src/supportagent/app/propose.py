"""Proposal construction (10-wp WP-011): a routed decision becomes ActionSpecs.

``build_action_specs`` turns one route's action list into concrete
:class:`~supportagent.domain.model.action.ActionSpec` values targeting the
case's source-system coordinates. The target service must be one the catalog
knows (the gate rejects an unknown service before any write); the scope is the
literal ``servicenow:<table>:<sys_id>:<kind>``; and the v0.0 work-note text is
a fixed template of case fields — never LLM free text (10-wp WP-011).

It is pure application code: no adapters, no I/O.
"""

from __future__ import annotations

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.routing import Route

#: v0.0 work-note template id (fixed; 10-wp WP-011).
_WORK_NOTE_TEMPLATE_ID = "work_note_v1"


def build_action_specs(route: Route, case: Case, catalog: Catalog) -> list[ActionSpec]:
    """One :class:`ActionSpec` per action the route requests, targeting the case.

    The target service must resolve in ``catalog`` (else the gate would reject
    it before any write); the scope is the literal
    ``servicenow:<table>:<sys_id>:<kind>`` (10-wp WP-011). The work-note payload
    is a fixed template of case fields — never LLM free text.
    """
    service = case.service
    known = {entry.name for entry in catalog.services}
    if service is None or service not in known:
        raise DomainValidationError(  # noqa: TRY003 — a routed case must have a service
            "build_action_specs requires a catalog-resolved service"
        )
    external_id = case.external_id
    if external_id is None:
        raise DomainValidationError(  # noqa: TRY003 — the scope references the sys_id
            "build_action_specs requires an external_id"
        )
    table = case.external_table or "incident"
    specs: list[ActionSpec] = []
    for kind in route.actions:
        specs.append(
            ActionSpec(
                kind=kind,
                target=TargetRef(service=service, system="servicenow", ref=external_id),
                scope=f"servicenow:{table}:{external_id}:{kind.value}",
                payload=_payload_for(kind, case),
            )
        )
    return specs


def _payload_for(kind: ActionKind, case: Case) -> dict[str, object]:
    if kind is ActionKind.WORK_NOTE:
        return {"template_id": _WORK_NOTE_TEMPLATE_ID, "text": _work_note_text(case)}
    # v0.0 proposes work notes only; other kinds carry an empty (hashable) payload.
    return {}


def _work_note_text(case: Case) -> str:
    """A fixed template of case fields — never LLM free text (10-wp WP-011)."""
    service = case.service or "unresolved"
    severity = case.severity.value if case.severity is not None else "unknown"
    classification = case.classification or "unclassified"
    return f"{service} / {severity} / {classification}"
