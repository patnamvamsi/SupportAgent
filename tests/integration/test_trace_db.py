"""ADR-0021 on the database: `supportagent trace` end to end with file logs.

A reviewed flow over three services' logs. The trace reads each stage through
the real ``logs.grep`` tool (the catalog bounds the paths, the id fills the
reviewed pattern) and writes every lookup and the trace itself to the event
log. A flow that reads business records is refused until an admin allows it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from supportagent.bootstrap import load_flows, trace_object
from supportagent.domain.errors import ConfigError
from tests.unit.test_pii import RegexAnalyzer
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _stage(app: str, *, within: int | None = 30, **extra: Any) -> dict[str, Any]:
    return {
        "id": app,
        "title": app.title(),
        "component": f"service:{app}",
        "lookup": {
            "capability": "logs.grep",
            "source": "logs",
            "args": {"path": "$service.log_paths[0]", "pattern": "trade $object.id "},
            "found": "matches[]",
            "fields": ["matches[].text"],
        },
        "within_min": within,
        **extra,
    }


def _setup(tmp_path: Path, *, records: bool = False) -> Any:
    logs = tmp_path / "logs"
    for app, lines in {
        "submit": ["trade T-1234 submitted by Jane Doe", "trade T-9999 submitted"],
        "enrich": ["trade T-1234 enriched", "trade T-9999 REJECTED: no counterparty"],
        "settle": [],
    }.items():
        (logs / app).mkdir(parents=True)
        (logs / app / "app.log").write_text("\n".join(lines) + "\n")
    catalog = tmp_path / "services.yaml"
    catalog.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "services": [{"name": a, "log_paths": [a]} for a in ("submit", "enrich", "settle")],
            }
        )
    )
    flows = tmp_path / "flows"
    flows.mkdir()
    settle = _stage("settle", within=None, terminal=True)
    if records:
        settle["lookup"] = {
            **settle["lookup"],
            "source": "records",
            "records_reason": "settlement status is only in the settlement table",
        }
    flow = {
        "flow": "trade-settlement",
        "title": "Trade from submission to settlement",
        "object": "trade",
        "id_pattern": r"T-\d{4}",
        "reviewed": True,
        "stages": [
            _stage("submit", within=None),
            _stage(
                "enrich",
                rejection={"path": "matches[].text", "values": ["REJECTED"], "match": "contains"},
            ),
            settle,
        ],
    }
    (flows / "trade-settlement.yaml").write_text(yaml.safe_dump(flow))
    base = _shadow_config()
    return base.model_copy(
        update={
            "paths": base.paths.model_copy(update={"catalog": str(catalog)}),
            "tools": base.tools.model_copy(update={"logs_root": str(logs)}),
            "tracing": base.tracing.model_copy(update={"flows_dir": str(flows)}),
        }
    )


def _kinds(db: Engine) -> list[str]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT kind FROM events"))]


def test_trace_end_to_end(tmp_path: Path, db: Engine, master_key: bytes) -> None:
    config = _setup(tmp_path)
    assert list(load_flows(config)) == ["trade-settlement"]
    run, answer = trace_object(
        config,
        "trade-settlement",
        "T-1234",
        engine=db,
        master_key=master_key,
        pii=RegexAnalyzer(),
        explain=False,
    )
    assert answer is None
    # No time limit on settlement: waiting there, never called stuck.
    assert (run.verdict.kind, run.verdict.stage) == ("in_progress", "settle")
    assert [o.state for o in run.observations] == ["found", "found", "missing"]
    text_seen = run.evidence["submit"]["matches"][0]["text"]  # type: ignore[index]
    assert text_seen == "trade T-1234 submitted by <PERSON_1>"
    assert _kinds(db).count("tool_call") == 3
    assert _kinds(db).count("trace") == 1

    rejected, _ = trace_object(
        config, "trade-settlement", "T-9999", engine=db, master_key=master_key, explain=False
    )
    assert (rejected.verdict.kind, rejected.verdict.stage) == ("rejected", "enrich")
    assert rejected.evidence == {}  # pii.engine is off: nothing for the model


def test_record_flows_need_an_admin(tmp_path: Path, db: Engine, master_key: bytes) -> None:
    config = _setup(tmp_path, records=True)
    with pytest.raises(ConfigError, match=r"tracing\.record_flows"):
        trace_object(config, "trade-settlement", "T-1234", engine=db, master_key=master_key)
    allowed = config.model_copy(
        update={"tracing": config.tracing.model_copy(update={"record_flows": ["trade-settlement"]})}
    )
    run, _ = trace_object(
        allowed, "trade-settlement", "T-1234", engine=db, master_key=master_key, explain=False
    )
    assert run.verdict.stage == "settle"


def test_flow_cli_validates(tmp_path: Path) -> None:
    from supportagent.entrypoints.cli.main import app  # noqa: PLC0415

    config = _setup(tmp_path)
    good = Path(config.tracing.flows_dir) / "trade-settlement.yaml"
    bad = tmp_path / "bad.yaml"
    bad.write_text("flow: x\n")
    res = CliRunner().invoke(app, ["flow", "validate", str(good), str(bad)])
    assert res.exit_code == 2
    assert "ok   " in res.output and "trade-settlement, 3 stages" in res.output
    assert "FAIL " in res.output


class _SlotProvider:
    """A provider answering each trace slot by its prompt; it keeps every request."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def complete_json(self, request: Any) -> Any:
        from supportagent.ports.llm import ProviderResponse  # noqa: PLC0415

        self.requests.append(request)
        if "explain where one business object is" in request.system:
            body = (
                '{"engineer": [{"text": "Trade T-1234 was submitted and enriched; '
                'settlement has no record of it.", "cites": ["S1", "S2", "S3"]}], '
                '"business": [{"text": "Your trade was accepted but has not reached '
                'settlement yet.", "cites": ["V"]}]}'
            )
        elif "check whether facts support one sentence" in request.system:
            body = '{"verdict": "supports"}'
        else:
            body = '{"hypotheses": []}'
        return ProviderResponse(
            raw_text=body, resolved_model="fake-model", tokens_in=1, tokens_out=1, latency_ms=1
        )


def test_explained_trace_sends_no_pii_to_the_model(
    tmp_path: Path, db: Engine, master_key: bytes
) -> None:
    base = _setup(tmp_path)
    slot = base.llm.slots["classify"]
    slots = {**base.llm.slots, "explain_trace": slot, "check_trace": slot}
    config = base.model_copy(update={"llm": base.llm.model_copy(update={"slots": slots})})
    provider = _SlotProvider()
    run, answer = trace_object(
        config,
        "trade-settlement",
        "T-1234",
        engine=db,
        master_key=master_key,
        pii=RegexAnalyzer(),
        llm_provider=provider,
    )
    assert answer is not None
    assert answer.summary == "Your trade was accepted but has not reached settlement yet."
    assert [s.cites for s in answer.engineer] == [["S1", "S2", "S3"]]
    sent = " ".join(r.system + r.user for r in provider.requests)
    assert "<PERSON_1>" in sent  # the evidence reached the model, pseudonymised
    assert "Jane" not in sent and "Doe" not in sent
    assert run.verdict.kind == "in_progress"
