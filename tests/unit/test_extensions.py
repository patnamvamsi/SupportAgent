"""WP-M09: analyst-authored extensions (14-wp; ADR-0011 §6).

The generators write scaffolds that validate as generated; the limits are
structural (read/T0 only, no action kinds); the skill doc names every
contract an extension touches, so it can't drift from the schemas.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from supportagent.adapters.tools.mcp import PROFILES
from supportagent.app.extend import (
    GAPS,
    NeedsEngineeringError,
    pack_scaffold,
    scenario_from_incident,
    tool_scaffold,
)
from supportagent.bootstrap import (
    KNOWN_TOOL_SCHEMAS,
    LabPipeline,
    incident_scenario,
    load_lab,
    validate_pack,
    write_scaffold,
)
from supportagent.config.schema import McpServer, ToolBinding
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.knowledge import DocFrontMatter, SopMatter, SopStepMatter
from supportagent.domain.model.pack import AppliesTo, KnowledgeScope, PackManifest
from supportagent.domain.model.preclass import PreclassRule
from supportagent.domain.model.runbook import AppliesWhen, Probe, Runbook, RunbookStep
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.tools import schema_errors
from supportagent.entrypoints.cli.main import app
from supportagent.evals.lab import (
    LabDoc,
    LabIncident,
    LabScenario,
    LabTruth,
    WorldEntry,
    run_lab,
)
from tests.unit.test_lab import _config
from tests.unit.test_python_postgres_pack import _Oracle

_REPO = Path(__file__).resolve().parents[2]
_SKILL = _REPO / ".claude" / "skills" / "extend-supportagent" / "SKILL.md"


def _with_packs(packs_dir: Path) -> Any:
    cfg = _config()
    return cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"packs": str(packs_dir)})})


def test_pack_new_scaffold_validates(tmp_path: Path) -> None:
    """As generated: it loads, compiles, covers itself, and passes its own lab (oracle)."""
    scaffold = pack_scaffold("kafka-jvm", stack=["kafka", "jvm"], description="Kafka on the JVM")
    write_scaffold(scaffold, tmp_path)
    pack_dir = tmp_path / "packs" / "kafka-jvm"

    result = validate_pack(_config(), pack_dir)
    assert result.ok, result.problems
    assert any("unfinished generator text" in n for n in result.notes)
    strict = validate_pack(_config(), pack_dir, strict=True)
    assert not strict.ok  # the PR check fails until every "Replace me" is gone

    cfg = _with_packs(tmp_path / "packs")
    scenarios, _, _ = load_lab(cfg, "kafka-jvm")
    assert {s.split for s in scenarios} == {"tune", "holdout"}
    report = run_lab(
        scenarios,
        LabPipeline(cfg, provider=_Oracle(scenarios)),
        pack="kafka-jvm",
        mode="oracle",
        model_id="oracle",
    )
    assert all(s.passed for s in report.scores), [s.failures for s in report.scores]

    with pytest.raises(ConfigError, match="refusing to overwrite"):
        write_scaffold(scaffold, tmp_path)
    with pytest.raises(ConfigError, match="reserved"):
        pack_scaffold("common", stack=["x"], description="d")


def test_pack_validate_reports_gaps(tmp_path: Path) -> None:
    """A hypothesis without a check or a scenario, and a missing control, are problems."""
    write_scaffold(pack_scaffold("kafka-jvm", stack=["kafka"], description="d"), tmp_path)
    pack_dir = tmp_path / "packs" / "kafka-jvm"
    hyps = pack_dir / "hypotheses.yaml"
    hyps.write_text(hyps.read_text() + "  - id: kafka_jvm_consumer_lag\n")
    (pack_dir / "evals" / "kafka-jvm-control.yaml").unlink()
    problems = validate_pack(_config(), pack_dir).problems
    assert any("'kafka_jvm_consumer_lag' has no probe" in p for p in problems)
    assert any("kafka_jvm_consumer_lag" in p and "root cause" in p for p in problems)
    assert any("no control scenario" in p for p in problems)
    assert any("no holdout" in p for p in problems)


def _load_module(name: str, path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_tool_new_scaffold_has_schema_fake_and_contract_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter declares its schema and read/T0; its contract test passes on its fake."""
    scaffold = tool_scaffold("queue.depth", known=KNOWN_TOOL_SCHEMAS)
    assert set(scaffold.files) == {
        "src/supportagent/adapters/tools/queue_depth.py",
        "src/supportagent/adapters/fakes/tool_queue_depth.py",
        "tests/contract/test_queue_depth_contract.py",
    }
    write_scaffold(scaffold, tmp_path)
    adapter = _load_module(
        "supportagent.adapters.tools.queue_depth",
        tmp_path / "src/supportagent/adapters/tools/queue_depth.py",
        monkeypatch,
    )
    _load_module(
        "supportagent.adapters.fakes.tool_queue_depth",
        tmp_path / "src/supportagent/adapters/fakes/tool_queue_depth.py",
        monkeypatch,
    )
    tool_cls = adapter.QueueDepthTool
    assert tool_cls.name == "queue.depth"
    assert (tool_cls.trust_class, tool_cls.tier) == (TrustClass.READ, Tier.T0)
    assert schema_errors({"target": "x", "value": 1.0, "ok": True}, tool_cls.result_schema) == []
    contract = _load_module(
        "tests.contract.test_queue_depth_contract",
        tmp_path / "tests/contract/test_queue_depth_contract.py",
        monkeypatch,
    )
    tests = [getattr(contract, n) for n in dir(contract) if n.startswith("test_")]
    assert len(tests) == 3
    for test in tests:
        test()

    with pytest.raises(ConfigError, match="exists"):
        tool_scaffold("metrics.query", known=KNOWN_TOOL_SCHEMAS)
    binding = tool_scaffold(
        "metrics.query", known=KNOWN_TOOL_SCHEMAS, mcp_server="vm", mcp_tool="query"
    )
    snippet = yaml.safe_load(binding.files["config/snippets/metrics.query.mcp.yaml"])["tools"]
    server = snippet["mcp_servers"]["vm"]
    assert (server["trust_class"], server["tier"], server["tools"]) == ("read", "T0", ["query"])
    ToolBinding.model_validate(snippet["bindings"]["metrics.query"])
    with pytest.raises(ConfigError, match="not in the catalog"):
        tool_scaffold("queue.depth", known=KNOWN_TOOL_SCHEMAS, mcp_server="vm", mcp_tool="q")


#: The INV-06 canary (a gitleaks-allowlisted fixture, never inline in a test).
_CANARY_KEY = (_REPO / "tests/data/canaries/generic_api_key.txt").read_text().strip()
_INCIDENT = {
    "result": [
        {
            "number": "INC0042",
            "short_description": "orders-db for Acme Corp timing out",
            "description": (
                "Contact jane.doe@acme.example or +1 415 555 0100; host db7.acme.internal "
                f"10.2.3.4, token={_CANARY_KEY}"
            ),
            "priority": "2",
        }
    ]
}
_IDENTIFYING = ("acme", "jane.doe", "415 555", "10.2.3.4", "db7", _CANARY_KEY.split("=")[-1])


def test_scenario_from_incident_is_redacted(tmp_path: Path) -> None:
    """Nothing identifying reaches the file, and it won't load until the truth is filled in."""
    source = tmp_path / "inc.json"
    source.write_text(json.dumps(_INCIDENT))
    scaffold = incident_scenario(_config(), "python-postgres", source, scrub=["Acme Corp", "acme"])
    ((path, text),) = scaffold.files.items()
    assert path == "packs/python-postgres/evals/python-postgres-inc0042.yaml"
    lowered = text.lower()
    leaks = [s for s in _IDENTIFYING if s.lower() in lowered]
    assert not leaks, leaks
    doc = yaml.safe_load(text)
    assert doc["split"] == "holdout" and doc["incident"]["priority"] == "2"
    assert set(doc["world"]) <= set(KNOWN_TOOL_SCHEMAS)
    with pytest.raises(ValidationError):
        LabScenario.model_validate(doc)  # the ground-truth form isn't filled in yet

    with pytest.raises(ConfigError, match="no subject"):
        scenario_from_incident("p", {"description": "x"}, redact=str)

    # Error signatures survive: a dotted code path is not a host name.
    kept = scenario_from_incident(
        "p",
        {"subject": "s", "body": "psycopg.errors.DiskFull on orders-db.shop.svc.cluster.local"},
        redact=str,
    )
    (text,) = kept.files.values()
    assert "psycopg.errors.DiskFull on <HOST>" in text


def test_extension_cannot_declare_write_tier() -> None:
    """Read/T0 only: a generator refuses, a manifest can't name actions, MCP must be read."""
    for kw in ({"tier": "T1"}, {"trust_class": "write"}, {"trust_class": "execute", "tier": "T2"}):
        with pytest.raises(NeedsEngineeringError, match="engineering change"):
            tool_scaffold("queue.restart", known=KNOWN_TOOL_SCHEMAS, **kw)
    result = CliRunner().invoke(app, ["tool", "new", "queue.restart", "--tier", "T1"])
    assert result.exit_code == 2
    assert "route it to engineering" in result.output

    with pytest.raises(ValidationError):
        PackManifest.model_validate(
            {
                "id": "x-pack",
                "version": "0.1.0",
                "description": "d",
                "applies_to": {"stack": ["x"]},
                "actions": [{"kind": "restart_pod"}],
            }
        )
    with pytest.raises(ValidationError, match="only read MCP tools"):
        McpServer.model_validate(
            {"url": "https://mcp.test/mcp", "tools": ["t"], "trust_class": "execute", "tier": "T1"}
        )


_CONTRACTS: tuple[type[BaseModel], ...] = (
    PackManifest,
    AppliesTo,
    KnowledgeScope,
    PreclassRule,
    Probe,
    Runbook,
    RunbookStep,
    AppliesWhen,
    LabScenario,
    LabIncident,
    WorldEntry,
    LabTruth,
    LabDoc,
    DocFrontMatter,
    SopMatter,
    SopStepMatter,
    McpServer,
    ToolBinding,
)


def test_skill_doc_names_every_contract() -> None:
    """Every field, capability, profile and gap the doc covers is in it, so it can't drift."""
    doc = _SKILL.read_text()
    assert doc.startswith("---\nname: extend-supportagent\n")
    named = set(re.findall(r"`([^`]+)`", doc))
    missing = [
        f"{model.__name__}.{f}"
        for model in _CONTRACTS
        for f in model.model_fields
        if f not in named
    ]
    missing += [f"model {m.__name__}" for m in _CONTRACTS if m.__name__ not in doc]
    missing += [f"capability {c}" for c in KNOWN_TOOL_SCHEMAS if c not in named]
    missing += [f"profile {p}" for p in PROFILES if p not in named]
    missing += [f"gap {g.gap!r}" for g in GAPS if g.gap not in doc]
    missing += [f"command {g.command!r}" for g in GAPS if f"`{g.command}`" not in doc]
    assert not missing, missing


def test_cli_pack_new_then_validate(tmp_path: Path) -> None:
    runner = CliRunner()
    made = runner.invoke(
        app,
        [
            "pack",
            "new",
            "kafka-jvm",
            "--stack",
            "kafka",
            "--description",
            "d",
            "--root",
            str(tmp_path),
        ],
    )
    assert made.exit_code == 0, made.output
    pack_dir = str(tmp_path / "packs" / "kafka-jvm")
    config = str(_REPO / "supportagent.example.yaml")
    ok = runner.invoke(app, ["pack", "validate", pack_dir, "--config", config])
    assert ok.exit_code == 0, ok.output
    strict = runner.invoke(app, ["pack", "validate", "--strict", pack_dir, "--config", config])
    assert strict.exit_code == 1
    assert "unfinished generator text" in strict.output
