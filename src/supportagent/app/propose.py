"""Proposal construction (10-wp WP-011): a routed decision becomes ActionSpecs.

``build_action_specs`` turns one route's action list into concrete
:class:`~supportagent.domain.model.action.ActionSpec` values targeting the
case's source-system coordinates. The target service must be one the catalog
knows (the gate rejects an unknown service before any write); the scope is
literal — ``service:<name>:<kind>`` for T1 kinds, so a setup grant covers them
(ADR-0005), else ``servicenow:<table>:<sys_id>:<kind>``; and the v0.0
work-note text is a fixed template of case fields — never LLM free text
(10-wp WP-011).

It is pure application code: no adapters, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.routing import Route
from supportagent.domain.scopes import is_service_scoped, record_scope, service_scope
from supportagent.domain.sop import SopMatch, sop_scope

#: v0.0 work-note template id (fixed; 10-wp WP-011).
_WORK_NOTE_TEMPLATE_ID = "work_note_v1"
_ENRICH_PAGE_TEMPLATE_ID = "enrich_page_v1"
_PAGE_RECOMMENDED_TEMPLATE_ID = "page_recommended_v1"
_SOP_STEP_TEMPLATE_ID = "sop_step_v1"
_RCA_PAGE_TEMPLATE_ID = "enrich_page_rca_v1"


def build_action_specs(route: Route, case: Case, catalog: Catalog) -> list[ActionSpec]:
    """One :class:`ActionSpec` per action the route requests, targeting the case.

    The target service must resolve in ``catalog`` (else the gate would reject
    it before any write). The scope is literal (``domain.scopes``, ADR-0005);
    the target always names the concrete record. The work-note payload is a
    fixed template of case fields — never LLM free text.
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
    entry = next(e for e in catalog.services if e.name == service)
    specs: list[ActionSpec] = []
    for kind in route.actions:
        specs.append(
            ActionSpec(
                kind=kind,
                target=TargetRef(service=service, system="servicenow", ref=external_id),
                scope=(
                    service_scope(service, kind)
                    if is_service_scoped(kind)
                    else record_scope("servicenow", table, external_id, kind)
                ),
                payload=_payload_for(kind, case, entry.paging_integration),
            )
        )
    return specs


def _payload_for(kind: ActionKind, case: Case, integration: str | None) -> dict[str, object]:
    if kind is ActionKind.WORK_NOTE:
        return {"template_id": _WORK_NOTE_TEMPLATE_ID, "text": _work_note_text(case)}
    if kind is ActionKind.ENRICH_PAGE:
        # WP-112: attach to the page the customer's monitoring opened for this
        # alert (matched by its dedup key), with a fixed template of case fields.
        return {
            "template_id": _ENRICH_PAGE_TEMPLATE_ID,
            "integration": integration or "",
            "source_ref": case.external_id or "",
            "text": f"SupportAgent case {case.id}: {_work_note_text(case)}",
        }
    # v0.0 proposes work notes only; other kinds carry an empty (hashable) payload.
    return {}


def page_recommended_spec(case: Case, integration: str, why: str) -> ActionSpec:
    """The ``work_note`` that carries a ``page_recommended`` (ARCH §14): a recommendation.

    The agent never pages. The note tells a person that triage suggests a
    page and that no open one was found; they decide, in their own tool.
    """
    service = case.service or "unresolved"
    severity = case.severity.value if case.severity is not None else "unknown"
    provider = integration.split(":", 1)[0]
    text = (
        f"Page recommended: {_work_note_text(case)}. No open page was found on {integration} "
        f"({why}). SupportAgent never pages: a person decides whether to page in {provider}."
    )
    return ActionSpec(
        kind=ActionKind.WORK_NOTE,
        target=TargetRef(service=service, system="servicenow", ref=case.external_id or ""),
        scope=service_scope(service, ActionKind.WORK_NOTE),
        payload={
            "template_id": _PAGE_RECOMMENDED_TEMPLATE_ID,
            "text": text,
            "page_recommended": True,
            "severity": severity,
        },
    )


def sop_step_spec(
    match: SopMatch, *, service: str, external_id: str, integration: str | None
) -> ActionSpec | None:
    """ADR-0014: one SOP step as a proposal, or ``None`` when its kind can't apply here.

    The text is a fixed template around the SOP's reviewed (and redacted)
    step note, never model output. The scope is the SOP's own literal scope,
    which no setup grant covers, so the step waits for a human approval. An
    ``enrich_page`` step needs the service's paging integration.
    """
    step = match.step
    version = f" v{match.version}" if match.version else ""
    link = f" ({match.url})" if match.url else ""
    text = (
        f"SOP step for {match.hypothesis} (supported by tool checks): "
        f"{match.title}{version}, step {step.id}: {step.note}{link}"
    )
    provenance: dict[str, object] = {
        "template_id": _SOP_STEP_TEMPLATE_ID,
        "sop_doc": match.doc_id,
        "sop_hash": match.content_hash,
        "sop_step": step.id,
        "text": text,
    }
    if step.kind is ActionKind.ENRICH_PAGE:
        if not integration:
            return None
        payload = {**provenance, "integration": integration, "source_ref": external_id}
    elif step.kind is ActionKind.WORK_NOTE:
        payload = provenance
    else:
        return None
    return ActionSpec(
        kind=step.kind,
        target=TargetRef(service=service, system="servicenow", ref=external_id),
        scope=sop_scope(match),
        payload=payload,
    )


def rca_page_spec(  # noqa: PLR0913 — the case coordinates and the RCA's allowlisted fields
    *,
    case_id: str,
    service: str,
    external_id: str,
    integration: str,
    outcome: str,
    supported: str | None,
    confirmed_by: Sequence[str],
    tested: Sequence[str],
    sop_pending: int,
) -> ActionSpec:
    """BL-040 (ARCH §14): RCA's result on the on-call page, from allowlisted fields only.

    The outcome, taxonomy hypothesis ids, tool names and a count: never model
    text. A cause is called *confirmed* only when its claim passed ``entail``
    (``confirmed_by`` is empty otherwise). Same kind and scope as triage's
    ``enrich_page``: T1, so the service's setup grant covers it, and it only
    ever attaches to a page that is already open.
    """
    parts = [f"SupportAgent RCA for case {case_id}: {outcome}."]
    if supported is not None and confirmed_by:
        parts.append(f"Cause {supported}, confirmed by {', '.join(sorted(confirmed_by))}.")
    elif supported is not None:
        parts.append(f"Leading cause {supported}; its evidence check did not confirm it.")
    others = [h for h in dict.fromkeys(tested) if h != supported]
    if others:
        parts.append(f"Also checked: {', '.join(others)}.")
    if sop_pending:
        parts.append(f"{sop_pending} SOP step(s) await approval in SupportAgent.")
    return ActionSpec(
        kind=ActionKind.ENRICH_PAGE,
        target=TargetRef(service=service, system="servicenow", ref=external_id),
        scope=service_scope(service, ActionKind.ENRICH_PAGE),
        payload={
            "template_id": _RCA_PAGE_TEMPLATE_ID,
            "integration": integration,
            "source_ref": external_id,
            "text": " ".join(parts),
        },
    )


def _work_note_text(case: Case) -> str:
    """A fixed template of case fields — never LLM free text (10-wp WP-011)."""
    service = case.service or "unresolved"
    severity = case.severity.value if case.severity is not None else "unknown"
    classification = case.classification or "unclassified"
    return f"{service} / {severity} / {classification}"
