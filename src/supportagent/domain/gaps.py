"""Sensing the unknown (ADR-0017 §3, WP-A3): what no plugin covers, with its evidence.

Detection says which plugins match a system. These say what **nothing**
matched, so a person learns what to provide instead of meeting silence:

- **unclaimed repository**: a repository no built-in or declared extractor
  turned into a component of the graph, with the manifests it has
  (``go.mod``, ``Gemfile``) and the technology each one names;
- **unmapped CMDB class**: installed CIs of a class nothing maps;
- **uncaused incidents**: operational incidents (not plain requests) the
  incident map could give no known cause, weighted like a class;
- **unknown capability**: a pack check naming a capability no tool and no
  plugin declaration provides, so it can never run.

Each gap carries a suggestion: the plugin to write, or what to declare.
Pure: the caller gives the files, the graph, the CMDB and the map.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from supportagent.domain.graph import SystemGraph
from supportagent.domain.incident_map import IncidentMap, PastIncident

GapKind = Literal[
    "unclaimed_repo", "unmapped_cmdb_class", "uncaused_incidents", "unknown_capability"
]
#: Manifest names and the technology each one says a repository is. These
#: describe file formats, not stacks: a plugin still has to claim the repo.
MANIFESTS: Mapping[str, str] = {
    "go.mod": "go",
    "Gemfile": "ruby",
    "Cargo.toml": "rust",
    "composer.json": "php",
    "mix.exs": "elixir",
    "*.csproj": "dotnet",
    "*.fsproj": "dotnet",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "package.json": "nodejs",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "Pipfile": "python",
    "setup.py": "python",
    "Package.swift": "swift",
}
_REQUEST_LABELS = ("request/",)
_EVIDENCE = 3


@dataclass(frozen=True, slots=True)
class Gap:
    kind: GapKind
    subject: str
    evidence: tuple[str, ...]
    #: The share of incident weight it holds back, when known (0 otherwise).
    weight: float
    suggestion: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "evidence": list(self.evidence),
            "weight": self.weight,
            "suggestion": self.suggestion,
        }


def technology(filename: str) -> str | None:
    """What a manifest's name says its repository is (``go.mod`` -> ``go``)."""
    return next(
        (t for pattern, t in MANIFESTS.items() if fnmatch.fnmatchcase(filename, pattern)), None
    )


def unclaimed_repos(files: Iterable[str], graph: SystemGraph) -> list[Gap]:
    """Repositories (``<repo>/<path>`` keys) that no node of the graph cites as its source."""
    by_repo: dict[str, list[str]] = {}
    for key in files:
        repo, _, path = key.partition("/")
        by_repo.setdefault(repo, []).append(path)
    cited = {
        s.split(":", 1)[1].split("/", 1)[0]
        for n in graph.nodes.values()
        for s in n.sources
        if s.startswith("git:")
    }
    out = []
    for repo, paths in sorted(by_repo.items()):
        if repo in cited:
            continue
        found = sorted({(p, t) for p in paths if (t := technology(p.rsplit("/", 1)[-1]))})
        tech = sorted({t for _, t in found})
        evidence = tuple(f"{p} ({t})" for p, t in found[:_EVIDENCE]) or tuple(
            sorted(paths)[:_EVIDENCE]
        )
        name = tech[0] if len(tech) == 1 else "<stack>"
        out.append(
            Gap(
                "unclaimed_repo",
                repo,
                evidence,
                0.0,
                f"no plugin claims it{' (' + ', '.join(tech) + ')' if tech else ''}: "
                f"supportagent plugin new {name} --from {found[0][0] if found else '<manifest>'}",
            )
        )
    return out


def unmapped_cmdb_classes(
    cis: Iterable[tuple[str, str, str]], mapped: Collection[str]
) -> list[Gap]:
    """Installed CIs (``(name, class, install status)``) of a class nothing maps to a node."""
    seen: dict[str, list[str]] = {}
    for name, cls, status in cis:
        if status.lower() != "retired" and cls not in mapped:
            seen.setdefault(cls, []).append(name)
    return [
        Gap(
            "unmapped_cmdb_class",
            cls,
            (*sorted(names)[:_EVIDENCE], f"{len(names)} CI(s)"),
            0.0,
            f'declare it in a plugin: {{match: "cmdb:{cls}", node: {{id: ..., kind: ...}}}}',
        )
        for cls, names in sorted(seen.items())
    ]


def uncaused_incidents(incident_map: IncidentMap, incidents: Sequence[PastIncident]) -> list[Gap]:
    """Operational incidents with no known cause: the no-cause class, less plain requests."""
    by_number = {i.number: i for i in incidents}
    out = []
    for cls in incident_map.classes:
        if cls.cause is not None:
            continue
        unread = set(incident_map.unread)  # no reading isn't no cause: it is a model problem
        operational = [
            n
            for n in cls.incidents
            if n in by_number
            and n not in unread
            and not (
                (r := incident_map.readings.get(n)) and (r.label or "").startswith(_REQUEST_LABELS)
            )
        ]
        if not operational:
            continue
        cis = Counter(by_number[n].ci for n in operational)
        titles = [by_number[n].title for n in sorted(operational)[:_EVIDENCE]]
        top = ", ".join(f"{ci} ({k})" for ci, k in cis.most_common(_EVIDENCE))
        out.append(
            Gap(
                "uncaused_incidents",
                f"{len(operational)} incident(s) with no known cause",
                (f"on {top}", *titles),
                round(cls.share * len(operational) / max(1, len(cls.incidents)), 3),
                "a cause no plugin declares: add it to a plugin's causes, with a probe",
            )
        )
    return out


def unknown_capabilities(
    pack_tools: Mapping[str, Collection[str]], known: Collection[str]
) -> list[Gap]:
    """Capabilities a pack's checks name that no tool or plugin declaration provides."""
    out = []
    for pack, tools in sorted(pack_tools.items()):
        missing = sorted(set(tools) - set(known))
        if missing:
            out.append(
                Gap(
                    "unknown_capability",
                    pack,
                    tuple(missing),
                    0.0,
                    "declare each in its plugin (result_schema) and bind it to an MCP server",
                )
            )
    return out


__all__ = [
    "MANIFESTS",
    "Gap",
    "GapKind",
    "technology",
    "uncaused_incidents",
    "unclaimed_repos",
    "unknown_capabilities",
    "unmapped_cmdb_classes",
]
