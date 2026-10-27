"""Analyst-authored extensions (ADR-0011 §6, WP-M09): generators and their limits.

A support analyst closes a gap without writing core code. Each kind of gap has
one extension, and each extension has a generator that writes a scaffold that
already validates:

- **a stack the agent doesn't know**: a pack (``supportagent pack new``);
- **a cause it can't check**: a hypothesis plus probe in a pack
  (``pack validate`` after editing);
- **a signal it can't read**: a tool capability (``tool new``), first-party
  or bound to a registered MCP server;
- **a miss on a real incident**: a lab scenario (``scenario new
  --from-incident``), redacted, with a ground-truth form to fill in.

The limits are structural:

- every generated tool is ``read`` / ``T0`` (``test_extension_cannot_declare_write_tier``);
- a pack manifest can't name action kinds (it is strict, extra keys fail);
- a request that needs a new *action kind* is refused with
  :class:`NeedsEngineeringError`: writing to a system is a gate, grant and
  ADR change, never an extension.

The generators here are pure (text in, files out as a mapping); the CLI writes
them, and ``bootstrap.validate_pack`` proves them.
"""

# ruff: noqa: E501 — the generated files' templates keep their own lines whole

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from supportagent.domain.errors import ConfigError

_PACK_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_SCENARIO_ID = re.compile(r"[^a-z0-9-]+")
#: A scenario goes into git, so beyond the secret detectors (INV-06) the
#: identifying details go too: addresses, phone numbers and host names.
_GIT_SCRUB: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b"), "<IP>"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.IGNORECASE), "<IP>"),
    (re.compile(r"(?<![\w<])\+?\d[\d ().-]{7,}\d\b"), "<PHONE>"),
    (re.compile(r"\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b", re.IGNORECASE), "<HOST>"),
)
#: The only trust class and tier an extension may declare (ARCH §7, ADR-0011 §6).
EXTENSION_TRUST_CLASS = "read"
EXTENSION_TIER = "T0"


class NeedsEngineeringError(ConfigError):
    """The request needs a change only engineering can make (an action kind, a write tier)."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what}: extensions are read-only (trust class read, tier T0). A new action "
            "kind or a write needs an engineering change: an action kind, its gate and "
            "grant rules, and an ADR. Open an issue describing the gap and route it to "
            "engineering (see .claude/skills/extend-supportagent/SKILL.md)."
        )


@dataclass(frozen=True, slots=True)
class Gap:
    """One row of the gap-to-extension table (the skill doc renders it)."""

    gap: str
    extension: str
    command: str


GAPS: tuple[Gap, ...] = (
    Gap(
        "the agent doesn't know a stack (Spark jobs, a JVM service)",
        "a new pack: labels, hypotheses, probes, runbooks, a classify fragment, lab scenarios",
        "supportagent pack new <id> --stack <tags>",
    ),
    Gap(
        "it misses a cause it could check with an existing tool",
        "a hypothesis and its probe (and a runbook step) in the pack",
        "edit the pack, then supportagent pack validate packs/<id>",
    ),
    Gap(
        "it mislabels a symptom",
        "a preclass rule or a classify fragment line in the pack",
        "edit the pack, then supportagent pack validate packs/<id>",
    ),
    Gap(
        "it can't read a signal (a metric store, a queue, an API)",
        "a read-only tool capability: first-party, or a binding to a registered MCP server",
        "supportagent tool new <capability> [--mcp <server> --mcp-tool <tool>]",
    ),
    Gap(
        "it got a real incident wrong",
        "a lab scenario from the incident, redacted, with its ground truth",
        "supportagent scenario new --pack <id> --from-incident <file>",
    ),
    Gap(
        "it should *do* something new (restart, rerun, page, edit a ticket field)",
        "none: a new action kind is an engineering change (gate, grants, ADR)",
        "refused: NeedsEngineeringError",
    ),
)


@dataclass(frozen=True, slots=True)
class Scaffold:
    """Files to write (relative path to text) and what the analyst does next."""

    files: Mapping[str, str]
    next_steps: Sequence[str] = field(default_factory=tuple)


def _yaml_str(value: str) -> str:
    """A YAML scalar for any string (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def _yaml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_yaml_str(v) for v in values) + "]"


def _slug(pack_id: str) -> str:
    return pack_id.replace("-", "_")


def pack_scaffold(pack_id: str, *, stack: Sequence[str], description: str) -> Scaffold:
    """A pack with one label, hypothesis, probe, rule, runbook and two scenarios.

    It loads, compiles and passes its own lab with an oracle as generated
    (``test_pack_new_scaffold_validates``). Only services whose catalog
    ``stack:`` names its tags select it, and ``pack validate --strict`` (the
    PR check) fails while any "Replace me" text is left.
    """
    if not _PACK_ID.match(pack_id) or pack_id == "common":
        raise ConfigError(  # noqa: TRY003
            f"pack id {pack_id!r}: lowercase letters, digits and '-', 2-41 characters; "
            "'common' is reserved"
        )
    tags = [t.strip().lower() for t in stack if t.strip()]
    if not tags:
        raise ConfigError("a pack needs at least one --stack tag, or no service selects it")  # noqa: TRY003
    slug = _slug(pack_id)
    label = f"{slug}/failure"
    hypothesis = f"{slug}_error_burst"
    root = f"packs/{pack_id}"
    service = {
        "name": f"{pack_id}-service",
        "stack": tags,
        "log_paths": [f"logs/{pack_id}-service"],
    }
    files = {
        f"{root}/pack.yaml": f"""# Generated by `supportagent pack new` (WP-M09). Replace the example
# knowledge it marks, then `supportagent pack validate --strict {root}`
# and the lab. Services whose catalog stack names these tags select it.
id: {pack_id}
version: 0.1.0
description: {_yaml_str(description)}
applies_to:
  stack: {_yaml_list(tags)}
taxonomy: taxonomy.yaml
hypotheses: hypotheses.yaml
preclass: preclass_rules.yaml
runbooks: runbooks/
probes: probes.yaml
prompts: {{classify: classify.md}}
tools:
  - logs.grep
evals: evals/
""",
        f"{root}/taxonomy.yaml": f"""# The labels this pack adds (ids are <domain>/<name>, unique across packs).
version: 0.1.0
labels:
  - id: {label}
    description: >-
      Replace me: the symptom family this label covers, in the words a ticket uses.
""",
        f"{root}/hypotheses.yaml": f"""# Root causes the pack can test. Each needs a probe or a runbook step.
version: 0.1.0
hypotheses:
  - id: {hypothesis}
""",
        f"{root}/probes.yaml": f"""# One probe per hypothesis: a read-only tool call and the check that must hold.
probes:
  {hypothesis}:
    tool: logs.grep
    args: {{path: "$service.log_paths[0]", pattern: "ERROR|Exception"}}
    check: "result.count > 10"
""",
        f"{root}/preclass_rules.yaml": f"""# Cheap deterministic signals: the first matching rule labels the case.
version: 0.1.0
rules:
  - when: {{body: "Replace me: a regex for this stack's signature error"}}
    label: {label}
""",
        f"{root}/runbooks/{pack_id}-failure.yaml": f"""# The checks to run, in order, for a case with this label (ARCH §5.1).
id: runbook:{pack_id}-failure
applies_when: {{classification: {label}}}
budget: {{max_tool_calls: 3, max_tokens: 2000, max_hypotheses: 2}}
steps:
  - id: error-burst
    hypothesis: {hypothesis}
    tool: logs.grep
    args: {{path: "$service.log_paths[0]", pattern: "ERROR|Exception"}}
    check: "result.count > 10"
""",
        f"{root}/classify.md": f"""How symptoms map to labels for {description}:
- Replace me: the symptoms that mean {label}, and the ones that look similar but aren't.
""",
        f"{root}/evals/{pack_id}-error-burst.yaml": _scenario_yaml(
            scenario_id=f"{pack_id}-error-burst",
            pack_id=pack_id,
            split="tune",
            description="Example: an error burst in the service log explains the alert.",
            subject=f"{pack_id}-service errors",
            body="Replace me: the alert or ticket text.",
            service=service,
            world={
                "logs.grep": {
                    "result": {
                        "path": f"logs/{pack_id}-service",
                        "count": 42,
                        "matches": [
                            {"file": "app.log", "line_no": 7, "text": "ERROR request failed"}
                        ],
                    }
                }
            },
            truth={
                "classification": label,
                "severity": "P3",
                "root_cause": hypothesis,
                "verdict": "SUPPORTED",
                "must_cite": ["logs.grep"],
            },
            why="Replace me: why this is the right answer, in one or two sentences.",
        ),
        f"{root}/evals/{pack_id}-control.yaml": _scenario_yaml(
            scenario_id=f"{pack_id}-control",
            pack_id=pack_id,
            split="holdout",
            description="Control: same symptom, nothing in the logs; the right answer claims no cause.",
            subject=f"{pack_id}-service slow",
            body="Replace me: a symptom with no findable cause.",
            service=service,
            world={
                "logs.grep": {
                    "result": {"path": f"logs/{pack_id}-service", "count": 0, "matches": []}
                }
            },
            truth={
                "classification": label,
                "severity": "P3",
                "root_cause": None,
                "verdict": "CONSISTENT_UNCONFIRMED",
                "must_not_claim": [hypothesis],
            },
            why="Every check ran and came back negative, so no cause is claimed.",
        ),
        f"{root}/README.md": f"""# {pack_id}

{description}

Generated by `supportagent pack new`. Next:

1. Replace the example label, hypothesis, probe, rule, runbook and classify text.
2. Add a lab scenario per hypothesis, at least one control (no findable cause)
   and one holdout: `supportagent scenario new --pack {pack_id} --from-incident <file>`.
3. `supportagent pack validate --strict {root}` until it is clean.
4. `supportagent eval lab --pack {pack_id} --live` from the LAN, commit the recordings.
5. Open the PR with the lab report (the PR template asks for it).
""",
    }
    return Scaffold(
        files=files,
        next_steps=(
            f"edit {root}/ (every 'Replace me')",
            f"supportagent pack validate {root}",
            f"supportagent eval lab --pack {pack_id} --live",
        ),
    )


def _scenario_yaml(  # noqa: PLR0913 — one scenario's fields
    *,
    scenario_id: str,
    pack_id: str,
    split: str,
    description: str,
    subject: str,
    body: str,
    service: Mapping[str, object],
    world: Mapping[str, object],
    truth: Mapping[str, object],
    why: str,
    header: str = "# Lab scenario (ADR-0011 §5). Edit only with a `why` note.",
) -> str:
    """A scenario as YAML (JSON flow values are valid YAML, so no YAML library is needed)."""
    lines = [
        header,
        f"id: {scenario_id}",
        f"pack: {pack_id}",
        f"split: {split}",
        f"description: {_yaml_str(description)}",
        "incident:",
        f"  subject: {_yaml_str(subject)}",
        f"  body: {_yaml_str(body)}",
        "  priority: '3'",
        f"service: {json.dumps(service, ensure_ascii=False)}",
        f"world: {json.dumps(world, ensure_ascii=False)}",
        "truth:",
        *(f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in truth.items()),
        f"why: {_yaml_str(why)}",
    ]
    return "\n".join(lines) + "\n"


def tool_scaffold(  # noqa: PLR0913 — the capability and its backend
    capability: str,
    *,
    known: Mapping[str, Mapping[str, object]],
    trust_class: str = EXTENSION_TRUST_CLASS,
    tier: str = EXTENSION_TIER,
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
) -> Scaffold:
    """A read-only tool capability: a first-party adapter, or an MCP binding.

    First-party: the adapter (``result_schema``, read/T0, an HTTP client), its
    fake (the canned upstream responses), and a contract test that checks
    the adapter's result against its schema on that fake. MCP: a config
    snippet binding an *existing* capability (it brings the schema) to a
    tool on a registered server.
    """
    if trust_class != EXTENSION_TRUST_CLASS or tier != EXTENSION_TIER:
        raise NeedsEngineeringError(f"capability {capability!r} as {trust_class}/{tier}")  # noqa: TRY003
    if not _CAPABILITY.match(capability):
        raise ConfigError(  # noqa: TRY003
            f"capability {capability!r}: <area>.<verb>, lowercase (e.g. queue.depth)"
        )
    if mcp_server is not None or mcp_tool is not None:
        return _mcp_binding(capability, known=known, server=mcp_server, tool=mcp_tool)
    if capability in known:
        raise ConfigError(  # noqa: TRY003
            f"capability {capability!r} exists; bind it to another backend with --mcp instead"
        )
    return _first_party(capability)


def _mcp_binding(
    capability: str,
    *,
    known: Mapping[str, Mapping[str, object]],
    server: str | None,
    tool: str | None,
) -> Scaffold:
    if capability not in known:
        raise ConfigError(  # noqa: TRY003
            f"capability {capability!r} is not in the catalog; an MCP binding needs its result "
            "schema, so add the capability first (tool new without --mcp)"
        )
    if not server or not tool:
        raise ConfigError("an MCP binding needs both --mcp <server> and --mcp-tool <tool>")  # noqa: TRY003
    snippet = f"""# Generated by `supportagent tool new {capability} --mcp {server}` (WP-M06/M09).
# Merge into your config's `tools:` block. The server is registered read-only:
# declared tools only, trust class read, tier T0 (anything else is refused).
tools:
  mcp_servers:
    {server}:
      url: "https://replace-me.example/mcp"
      token_env: {server.upper().replace("-", "_")}_MCP_TOKEN
      tools: [{tool}]
      trust_class: read
      tier: T0
  bindings:
    {capability}:
      mcp: {server}
      tool: {tool}
      profile: passthrough
"""
    return Scaffold(
        files={f"config/snippets/{capability}.mcp.yaml": snippet},
        next_steps=(
            "merge the snippet into the config and set the token in the environment",
            "supportagent doctor  (the mcp check verifies the server's annotations)",
            "write a reply profile if the tool's reply isn't the capability's schema",
        ),
    )


def _first_party(capability: str) -> Scaffold:
    area, verb = capability.split(".")
    module = f"{area}_{verb}"
    cls = "".join(p.capitalize() for p in re.split(r"[_.]", capability)) + "Tool"
    src = f"src/supportagent/adapters/tools/{module}.py"
    fake = f"src/supportagent/adapters/fakes/tool_{module}.py"
    test = f"tests/contract/test_{module}_contract.py"
    adapter = f'''"""``{capability}`` (WP-M09 scaffold): replace this line with what the tool reads.

A first-party read tool (ARCH §7): trust class read, tier T0, a declared
result schema (pack checks compile against it), and arguments bound by the
registry from catalog fields or reviewed literals, never from ticket text.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

SCHEMA: Mapping[str, object] = {{
    "type": "object",
    "required": ["target", "value", "ok"],
    "properties": {{
        "target": {{"type": "string"}},
        "value": {{"type": "number"}},
        "ok": {{"type": "boolean"}},
    }},
}}


class {cls}:
    name = "{capability}"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        target = args.get("target")
        if not isinstance(target, str) or not target:
            raise ToolError("{capability}: target is required")  # noqa: TRY003
        body = get_json(self._client, f"/status/{{target}}", timeout_s=timeout_s)
        if not isinstance(body, dict):
            raise ToolError("{capability}: unexpected response")  # noqa: TRY003
        value = float(body.get("value", 0.0))
        return ToolResult({{"target": target, "value": value, "ok": bool(body.get("ok", True))}})


__all__ = ["SCHEMA", "{cls}"]
'''
    fake_src = f'''"""A fake upstream for ``{capability}`` (WP-M09 scaffold): canned API replies."""

from __future__ import annotations

import httpx


def fake_client(replies: dict[str, object] | None = None) -> httpx.Client:
    """An ``httpx.Client`` whose upstream serves ``replies`` by path (404 otherwise)."""
    routes = replies if replies is not None else {{"/status/orders": {{"value": 12.5, "ok": True}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, json={{}})

    return httpx.Client(base_url="https://fake.test", transport=httpx.MockTransport(handler))
'''
    test_src = f'''"""Contract: ``{capability}`` returns its declared schema on the fake (WP-M09 scaffold)."""

from __future__ import annotations

import pytest

from supportagent.adapters.tools.{module} import {cls}
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.tools import schema_errors
from supportagent.adapters.fakes.tool_{module} import fake_client


def test_{module}_result_matches_declared_schema() -> None:
    tool = {cls}(client=fake_client())
    result = tool.run({{"target": "orders"}}, timeout_s=2.0)
    assert schema_errors(result.data, tool.result_schema) == []


def test_{module}_is_read_only() -> None:
    assert ({cls}.trust_class, {cls}.tier) == (TrustClass.READ, Tier.T0)


def test_{module}_upstream_error_is_a_tool_error() -> None:
    with pytest.raises(ToolError):
        {cls}(client=fake_client({{}})).run({{"target": "missing"}}, timeout_s=2.0)
'''
    return Scaffold(
        files={src: adapter, fake: fake_src, test: test_src},
        next_steps=(
            f"implement {src} against the real API (keep the schema honest)",
            f"register {cls} in adapters/tools/capabilities.py (_FIRST_PARTY) and in "
            "bootstrap's tool registry",
            f"pytest {test}",
            "use it from a pack: tools: [..., " + capability + "] and a probe",
        ),
    )


def scenario_from_incident(  # noqa: PLR0913 — the incident and the pack's context
    pack_id: str,
    incident: Mapping[str, object],
    *,
    redact: Callable[[str], str],
    scrub: Sequence[str] = (),
    service: Mapping[str, object] | None = None,
    tools: Sequence[str] = (),
    split: str = "holdout",
) -> Scaffold:
    """A lab scenario from a real incident: redacted, with a ground-truth form.

    ``incident`` is a ServiceNow-style row (``short_description``,
    ``description``, ``priority``, ``number``) or a lab-style one (``subject``,
    ``body``, ``priority``). Text passes the redactor (INV-06), every
    ``scrub`` term (customer names) is replaced, and so are IP addresses,
    phone numbers and host names, so nothing identifying reaches git. ``truth`` is left for the analyst: the file
    fails to load until it's filled in, so an unreviewed scenario can't
    enter the lab. New scenarios default to ``holdout`` (06 tuning hygiene).
    """
    if split not in ("tune", "holdout"):
        raise ConfigError(f"split must be tune or holdout, not {split!r}")  # noqa: TRY003

    def clean(value: object) -> str:
        text = redact(str(value or ""))
        for pattern, placeholder in _GIT_SCRUB:
            text = pattern.sub(placeholder, text)
        for term in scrub:
            if term:
                text = re.sub(re.escape(term), "<redacted>", text, flags=re.IGNORECASE)
        return text.strip()

    subject = clean(incident.get("subject") or incident.get("short_description"))
    if not subject:
        raise ConfigError("the incident has no subject or short_description")  # noqa: TRY003
    body = clean(incident.get("body") or incident.get("description"))
    number = clean(incident.get("number") or "incident")
    scenario_id = _SCENARIO_ID.sub("-", f"{pack_id}-{number}".lower()).strip("-")[:80]
    svc = dict(service) if service else {"name": "replace-me", "stack": []}
    world = {
        name: {"result": "FILL IN: what this tool returned, in its result schema"} for name in tools
    }
    header = (
        "# Generated by `supportagent scenario new --from-incident` (WP-M09), redacted.\n"
        "# Fill in every FILL IN: the world each tool returned, and the ground truth\n"
        "# (truth: classification, severity, root_cause or null, verdict, must_cite,\n"
        "# must_not_claim, also_acceptable). It won't load until truth is complete."
    )
    text = _scenario_yaml(
        scenario_id=scenario_id,
        pack_id=pack_id,
        split=split,
        description="FILL IN: one line on what happened",
        subject=subject,
        body=body,
        service=svc,
        world=world,
        truth={
            "classification": "FILL IN",
            "severity": "FILL IN (P1-P4)",
            "root_cause": "FILL IN (a pack hypothesis id, or null)",
            "verdict": "FILL IN (SUPPORTED, CONSISTENT_UNCONFIRMED or INCONCLUSIVE)",
            "must_cite": [],
            "must_not_claim": [],
        },
        why="FILL IN: why this is the right answer",
        header=header,
    )
    priority = str(incident.get("priority") or "3")
    if re.fullmatch(r"[1-5]", priority):
        text = text.replace("  priority: '3'", f"  priority: '{priority}'", 1)
    return Scaffold(
        files={f"packs/{pack_id}/evals/{scenario_id}.yaml": text},
        next_steps=(
            "fill in every FILL IN (world results and truth)",
            f"supportagent pack validate packs/{pack_id}",
        ),
    )


__all__ = [
    "EXTENSION_TIER",
    "EXTENSION_TRUST_CLASS",
    "GAPS",
    "Gap",
    "NeedsEngineeringError",
    "Scaffold",
    "pack_scaffold",
    "scenario_from_incident",
    "tool_scaffold",
]
