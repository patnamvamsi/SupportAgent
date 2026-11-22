"""Onboarding ingest (ADR-0015 P1, WP-O2): read a system's sources into a workspace.

From a ``system.yaml``, in order:

1. **the ticketing history**: incidents, problems, changes and the CMDB around
   them, read by the connector (ADR-0013: the queries and field names are
   the vendor's and live there);
2. **documentation**: every current page of each listed Confluence space;
3. **repositories**: the files matching each Git source's path globs.

Everything is read-only and **redacted at ingest** (INV-06) before it is
stored. Each source runs within the manifest's budget; a cap reached is
reported, never silent. Each finished source is recorded in the workspace,
so an interrupted run resumes where it stopped and ``refresh`` re-reads.

A recorded source is **resumed** only while it is current (ADR-0023): its
definition in the manifest is unchanged (paths, exclusions, scope, budget),
and it was read recently enough for its kind (:data:`MAX_AGE_S`: code moves
faster than ticket history). Otherwise it is read again, so a changed
manifest takes effect and the instance keeps up with the system's code.

The workspace has the onboarding lab's layout, so detection (WP-O1) and the
later stages read a real system exactly as they read a reference one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Protocol

from supportagent.config.system_manifest import ConfluenceSpace, GitSource, SystemManifest
from supportagent.domain.onboarding import (
    TICKET_TEXT_FIELDS,
    IngestedFiles,
    IngestedPage,
    TicketHistory,
)


class HistoryExport(Protocol):
    """The connector's read of the system's ticket history (scope bound by the caller)."""

    def __call__(self, *, max_incidents: int, now: datetime) -> TicketHistory: ...


class ConfluenceExport(Protocol):
    def __call__(
        self, space: ConfluenceSpace, *, max_pages: int
    ) -> tuple[list[IngestedPage], bool]: ...


class GitRead(Protocol):
    def __call__(
        self, source: GitSource, *, max_files: int, max_file_bytes: int
    ) -> IngestedFiles: ...


class _Workspace(Protocol):
    def prepare(self, manifest_text: str) -> None: ...
    def state(self) -> dict[str, object]: ...
    def mark(self, source: str, record: Mapping[str, object]) -> None: ...
    def write_records(
        self, name: str, rows: Sequence[Mapping[str, object]] | Mapping[str, object]
    ) -> None: ...
    def replace_space(self, space: str, pages: Sequence[IngestedPage]) -> None: ...
    def replace_repos(self, repos: Sequence[str], files: Mapping[str, str]) -> None: ...


#: How long a source read stays current, by kind.
MAX_AGE_S: Mapping[str, int] = {"git": 3600, "confluence": 86_400, "tickets": 86_400}


def _spec(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceReport:
    source: str
    #: ``read``, ``resumed`` (already done, skipped) or ``unavailable`` (no reader configured).
    status: str
    counts: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False
    notes: Sequence[str] = ()

    def line(self) -> str:
        counts = ", ".join(f"{n} {k}" for k, n in self.counts.items()) or "-"
        cap = " (budget reached: more exist)" if self.truncated else ""
        return f"{self.status:<11} {self.source}: {counts}{cap}"


class IngestService:
    def __init__(  # noqa: PLR0913 — one reader per source kind, the redactor, the clock
        self,
        *,
        workspace: _Workspace,
        redact: Callable[[str], str],
        now: Callable[[], datetime],
        history: HistoryExport | None = None,
        history_label: str | None = None,
        confluence: ConfluenceExport | None = None,
        git: GitRead | None = None,
    ) -> None:
        self._ws = workspace
        self._redact = redact
        self._now = now
        self._history = history
        #: The manifest's ticket source (its ``sources`` key), when it scopes one.
        self._history_label = history_label
        self._confluence = confluence
        self._git = git

    def run(
        self, manifest: SystemManifest, manifest_text: str, *, refresh: bool = False
    ) -> list[SourceReport]:
        self._ws.prepare(manifest_text)
        done = {} if refresh else self._ws.state()
        budget = manifest.budget
        reports: list[SourceReport] = []
        if self._history_label is not None:
            label = self._history_label
            scope = getattr(manifest.sources, label, None)  # the ticket source it names
            spec = _spec(scope.model_dump() if scope else None, budget.max_incidents)
            step = partial(self._tickets, label, manifest)
            reports.append(self._once(label, "tickets", spec, done, step))
        for space in manifest.sources.confluence:
            name = f"confluence:{space.space}"
            spec = _spec(space.model_dump(exclude={"user", "token"}), budget.max_pages)
            step = partial(self._space, space, manifest)
            reports.append(self._once(name, "confluence", spec, done, step))
        for source in manifest.sources.git:
            name = f"git:{source.repo}"
            spec = _spec(
                source.model_dump(exclude={"token"}), budget.max_files, budget.max_file_bytes
            )
            step = partial(self._repos, source, manifest)
            reports.append(self._once(name, "git", spec, done, step))
        return reports

    def _once(
        self,
        name: str,
        kind: str,
        spec: str,
        done: Mapping[str, object],
        step: Callable[[], SourceReport],
    ) -> SourceReport:
        previous = done.get(name)
        if (
            isinstance(previous, Mapping)
            and previous.get("done")
            and self._current(previous, kind, spec)
        ):
            counts = previous.get("counts")
            return SourceReport(name, "resumed", counts if isinstance(counts, dict) else {})
        report = step()
        if report.status == "read":
            self._ws.mark(
                name,
                {
                    "done": True,
                    "counts": dict(report.counts),
                    "truncated": report.truncated,
                    "at": self._now().isoformat(),
                    "spec": spec,
                },
            )
        return report

    def _current(self, previous: Mapping[str, object], kind: str, spec: str) -> bool:
        """Read under this definition of the source, recently enough for its kind."""
        if previous.get("spec") != spec:
            return False
        try:
            at = datetime.fromisoformat(str(previous.get("at")))
        except ValueError:
            return False
        return (self._now() - at).total_seconds() < MAX_AGE_S[kind]

    def _tickets(self, label: str, manifest: SystemManifest) -> SourceReport:
        if self._history is None:
            return SourceReport(label, "unavailable", notes=["no connection configured"])
        history = self._history(max_incidents=manifest.budget.max_incidents, now=self._now())
        incidents = [self._clean(r) for r in history.incidents]
        problems = [
            {**p, "short_description": self._redact(str(p.get("short_description", "")))}
            for p in history.problems
        ]
        changes = [self._clean(c) for c in history.changes]
        self._ws.write_records("incidents", incidents)
        self._ws.write_records("problems", problems)
        self._ws.write_records("changes", changes)
        self._ws.write_records("cmdb", {"cis": history.cis, "relationships": history.relationships})
        return SourceReport(
            label,
            "read",
            {
                "incidents": len(incidents),
                "problems": len(problems),
                "changes": len(changes),
                "cis": len(history.cis),
                "relationships": len(history.relationships),
            },
            truncated=history.truncated,
        )

    def _clean(self, record: Mapping[str, str]) -> dict[str, str]:
        out = dict(record)
        for key in TICKET_TEXT_FIELDS:
            if key in out:
                out[key] = self._redact(out[key])
        return out

    def _space(self, space: ConfluenceSpace, manifest: SystemManifest) -> SourceReport:
        name = f"confluence:{space.space}"
        if self._confluence is None:
            return SourceReport(name, "unavailable", notes=["no Confluence reader"])
        pages, more = self._confluence(space, max_pages=manifest.budget.max_pages)
        clean = [
            IngestedPage(
                page_id=p.page_id,
                title=self._redact(p.title),
                version=p.version,
                last_updated=p.last_updated,
                labels=p.labels,
                url=p.url,
                markdown=self._redact(p.markdown),
            )
            for p in pages
        ]
        self._ws.replace_space(space.space, clean)
        return SourceReport(name, "read", {"pages": len(clean)}, truncated=more)

    def _repos(self, source: GitSource, manifest: SystemManifest) -> SourceReport:
        name = f"git:{source.repo}"
        if self._git is None:
            return SourceReport(name, "unavailable", notes=["no repository reader"])
        found = self._git(
            source,
            max_files=manifest.budget.max_files,
            max_file_bytes=manifest.budget.max_file_bytes,
        )
        files = {k: self._redact(v) for k, v in found.files.items()}
        self._ws.replace_repos(found.repos, files)
        unreadable = sorted(k for k, why in found.skipped.items() if why.startswith("unreadable"))
        notes = [f"unreadable: {', '.join(unreadable[:5])}"] if unreadable else []
        notes += [f"skipped {k}: {why}" for k, why in sorted(found.skipped.items())][:10]
        counts = {"repos": len(found.repos), "files": len(files), "skipped": len(found.skipped)}
        if unreadable:
            counts["unreadable"] = len(unreadable)  # ADR-0023: a need, not a silent gap
        return SourceReport(
            name,
            "read",
            counts,
            truncated=found.truncated,
            notes=notes,
        )


__all__ = ["ConfluenceExport", "GitRead", "HistoryExport", "IngestService", "SourceReport"]
