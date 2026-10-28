"""ADR-0014 part 1: SOP-driven requests. A reviewed SOP proposes; the gate decides.

An SOP step applies only when the tools SUPPORT a hypothesis it names. Its
proposal uses an existing internal kind, a fixed template and its own
literal scope, which no setup grant covers.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.files.knowledge_loader import load_markdown_source
from supportagent.app.propose import sop_step_spec
from supportagent.app.rca import Budget
from supportagent.domain.errors import ConfigError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.knowledge import DocScope
from supportagent.domain.sop import Sop, SopMatch, SopStep, sop_scope
from supportagent.evals.lab import LabScenario, score_outcome
from tests.unit.test_knowledge import _SCOPE, _index, _meta, _rca, _redact
from tests.unit.test_lab import _outcome
from tests.unit.test_rca import FakeTools, _case

_SOP = Sop(
    when=("dependency_failure",),
    steps=(
        SopStep("tell-data-team", ActionKind.WORK_NOTE, "Ask the data team to rerun the DAG."),
        SopStep("note-page", ActionKind.ENRICH_PAGE, "Link the DAG run on the page."),
    ),
)
_SOP_DOC = (
    _meta(
        "wiki:nightly-sync-sop.md",
        title="Nightly sync SOP",
        version="7",
        url="https://wiki.example/sop/nightly-sync",
        hypotheses=("dependency_failure",),
        sop=_SOP,
    ),
    "# Nightly sync SOP\nWhen the nightly sync DAG fails upstream, follow these steps.",
)
_FAILED = {"dag_id": "nightly-sync", "runs": [], "failed_tasks": [{"task_id": "load"}]}
_QUIET = {"dag_id": "nightly-sync", "runs": [], "failed_tasks": []}
_TODAY = date(2026, 10, 21)


def test_sop_steps_apply_only_when_the_tools_support_the_cause() -> None:
    supported = _rca(_index(_SOP_DOC), FakeTools({"airflow.dag_runs": _FAILED})).run(
        _case(classification="network/dns", summary="nightly sync paging"), Budget()
    )
    assert supported.outcome == "SUPPORTED"
    assert [m.key for m in supported.sop_steps] == [
        "wiki:nightly-sync-sop.md/tell-data-team",
        "wiki:nightly-sync-sop.md/note-page",
    ]
    assert supported.sop_steps[0].ref()["hash"] == _SOP.digest
    # The doc suggested the check; a negative check means no SOP step, whatever the doc says.
    unconfirmed = _rca(_index(_SOP_DOC), FakeTools({"airflow.dag_runs": _QUIET})).run(
        _case(classification="network/dns", summary="nightly sync paging"), Budget()
    )
    assert unconfirmed.outcome != "SUPPORTED" and unconfirmed.sop_steps == []


def test_stale_or_out_of_scope_sops_propose_nothing() -> None:
    meta, body = _SOP_DOC
    stale = _index((_meta(**{**_kw(meta), "last_updated": date(2020, 1, 1)}), body))
    assert stale.sops_for("dependency_failure", _SCOPE, today=_TODAY) == []
    other = _index((_meta(**{**_kw(meta), "services": ("billing",)}), body))
    assert other.sops_for("dependency_failure", _SCOPE, today=_TODAY) == []
    elsewhere = DocScope(frozenset({"other-space"}), "payroll-api")
    assert _index(_SOP_DOC).sops_for("dependency_failure", elsewhere, today=_TODAY) == []
    assert _index(_SOP_DOC).sops_for("capacity_decay", _SCOPE, today=_TODAY) == []


def _kw(meta: Any) -> dict[str, Any]:
    return {f: getattr(meta, f) for f in meta.__dataclass_fields__ if f != "doc_id"} | {
        "doc_id": meta.doc_id
    }


def _match(step: SopStep) -> SopMatch:
    return SopMatch(
        "wiki:nightly-sync-sop.md",
        "Nightly sync SOP",
        "7",
        "https://wiki.example/sop/nightly-sync",
        _SOP.digest,
        "dependency_failure",
        step,
    )


def test_sop_proposal_is_a_fixed_template_with_its_own_scope() -> None:
    work_note, page = _SOP.steps
    spec = sop_step_spec(
        _match(work_note), service="payroll-api", external_id="sys-1", integration=None
    )
    assert spec is not None and spec.kind is ActionKind.WORK_NOTE
    assert spec.scope == f"sop:wiki:nightly-sync-sop.md@{_SOP.digest}:tell-data-team:work_note"
    assert spec.scope != "service:payroll-api:work_note"  # no setup grant covers it
    assert spec.payload["text"] == (
        "SOP step for dependency_failure (supported by tool checks): Nightly sync SOP v7, "
        "step tell-data-team: Ask the data team to rerun the DAG. "
        "(https://wiki.example/sop/nightly-sync)"
    )
    assert spec.payload["sop_hash"] == _SOP.digest
    # enrich_page needs the service's paging integration; without one it doesn't apply.
    assert (
        sop_step_spec(_match(page), service="payroll-api", external_id="sys-1", integration=None)
        is None
    )
    paged = sop_step_spec(
        _match(page), service="payroll-api", external_id="sys-1", integration="pagerduty:svc"
    )
    assert paged is not None and paged.payload["integration"] == "pagerduty:svc"


def test_an_edited_sop_is_a_new_scope() -> None:
    edited = Sop(
        _SOP.when, (SopStep("tell-data-team", ActionKind.WORK_NOTE, "Rerun it yourself."),)
    )
    assert edited.digest != _SOP.digest
    before = sop_scope(_match(_SOP.steps[0]))
    after = sop_scope(
        SopMatch("wiki:nightly-sync-sop.md", "t", "7", None, edited.digest, "x", edited.steps[0])
    )
    assert before != after


def _sop_md(steps: str) -> str:
    return (
        "---\ntitle: Nightly sync SOP\nsop:\n  when: [dependency_failure]\n  steps:\n"
        f"{steps}---\n# Nightly sync SOP\nFollow these steps.\n"
    )


def test_git_sop_loads_with_notes_redacted(tmp_path: Path) -> None:
    (tmp_path / "sop.md").write_text(
        _sop_md("    - {id: tell, kind: work_note, note: 'Page jane.doe@example.com first.'}\n")
    )
    [chunk] = load_markdown_source(tmp_path, space="wiki", redact=_redact)
    assert chunk.meta.sop is not None
    [step] = chunk.meta.sop.steps
    assert step.kind is ActionKind.WORK_NOTE and "jane.doe@example.com" not in step.note


@pytest.mark.parametrize(
    ("steps", "error"),
    [
        ("    - {id: fix, kind: ticket_update, note: 'Resolve it.'}\n", "may propose only"),
        ("    - {id: tell, kind: customer_comment, note: 'Tell them.'}\n", "may propose only"),
        ("    - {id: tell, kind: delete_database, note: 'No.'}\n", "kind"),
        (
            "    - {id: a, kind: work_note, note: x}\n    - {id: a, kind: work_note, note: y}\n",
            "unique",
        ),
    ],
    ids=["field-update", "customer-facing", "new-kind", "duplicate-step"],
)
def test_sop_steps_must_be_existing_internal_kinds(tmp_path: Path, steps: str, error: str) -> None:
    (tmp_path / "sop.md").write_text(_sop_md(steps))
    with pytest.raises(ConfigError, match=error):
        load_markdown_source(tmp_path, space="wiki", redact=_redact)


def test_lab_scores_the_proposed_sop_steps() -> None:
    scenario = LabScenario.model_validate(
        {
            "id": "sop-lab",
            "pack": "common",
            "description": "d",
            "incident": {"subject": "s", "body": "b"},
            "service": {"name": "payroll-api"},
            "docs": [
                {
                    "id": "sync-sop",
                    "title": "Nightly sync SOP",
                    "body": "Steps.",
                    "sop": {
                        "when": ["dependency_failure"],
                        "steps": [{"id": "tell", "kind": "work_note", "note": "Tell data."}],
                    },
                }
            ],
            "truth": {
                "classification": "data_pipeline/failure",
                "severity": "P3",
                "root_cause": "dependency_failure",
                "verdict": "SUPPORTED",
                "sop_steps": ["sync-sop/tell"],
            },
            "why": "w",
        }
    )
    base = {
        "scenario_id": "sop-lab",
        "classification": "data_pipeline/failure",
        "supported": "dependency_failure",
        "cited_tools": frozenset(),
    }
    good = score_outcome(scenario, _outcome(**base, sop_steps=("sync-sop/tell",)))
    assert good.sop_ok is True and good.passed
    missing = score_outcome(scenario, _outcome(**base, sop_steps=()))
    assert missing.sop_ok is False and not missing.passed
    with pytest.raises(ValueError, match="unknown SOP steps"):
        LabScenario.model_validate(
            {
                **scenario.model_dump(mode="json"),
                "truth": {**scenario.truth.model_dump(mode="json"), "sop_steps": ["sync-sop/x"]},
            }
        )
