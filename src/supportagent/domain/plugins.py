"""Plugin detection (ADR-0015 §2, WP-O1): which plugins does this system need?

Onboarding reads a system's sources (WP-O2) into a :class:`SourceSnapshot`:
repository files, current documentation, incident text, CMDB classes, where
its code lives, and which incidents a change caused. Each plugin's detectors
are scored against it. A plugin whose fired signals reach ``min_score`` is
**detected**, with the evidence that fired, so a person can see why it is
proposed ("pom.xml in 5 repos contains spring-boot").

Documentation counts only when it is current: pages labelled ``legacy``,
``draft`` or ``superseded`` (or titled "superseded") are left out, so a 2023
design page can't pull in a plugin for a database the system no longer uses.
Detection proposes; it never installs (that is a reviewed request, WP-O5).

Pure: the snapshot is built by the caller.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from supportagent.domain.model.plugin import PluginManifest, Signal

#: Labels that mark a page as not describing the system as it is now.
NOT_CURRENT_LABELS = frozenset({"legacy", "draft", "superseded", "archived"})
_EVIDENCE_SHOWN = 3


@dataclass(frozen=True, slots=True)
class DocPage:
    page_id: str
    title: str
    labels: frozenset[str]
    text: str

    @property
    def current(self) -> bool:
        return not (self.labels & NOT_CURRENT_LABELS) and "superseded" not in self.title.lower()


@dataclass(frozen=True, slots=True)
class IncidentText:
    number: str
    text: str
    caused_by_change: bool = False


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """What onboarding has read about one system (WP-O2 fills it)."""

    #: ``<repo>/<path>`` -> text
    files: Mapping[str, str] = field(default_factory=dict)
    docs: Sequence[DocPage] = ()
    incidents: Sequence[IncidentText] = ()
    #: CMDB CI classes of installed (not retired) CIs.
    cmdb_classes: frozenset[str] = frozenset()
    #: Where the manifest's repositories live (``github``, ``gitlab``, ...).
    git_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Fired:
    kind: str
    weight: float
    evidence: str


@dataclass(frozen=True, slots=True)
class Detection:
    plugin: str
    score: float
    threshold: float
    fired: tuple[Fired, ...]

    @property
    def detected(self) -> bool:
        return self.score >= self.threshold

    def explain(self) -> str:
        reasons = "; ".join(f.evidence for f in self.fired) or "no signal fired"
        return f"{self.plugin}: score {self.score:g}/{self.threshold:g}: {reasons}"


def detect(plugins: Sequence[PluginManifest], snapshot: SourceSnapshot) -> list[Detection]:
    """Every plugin's detection result, detected ones first, then by score."""
    out = []
    for plugin in plugins:
        fired = [f for s in plugin.detectors.signals if (f := _fire(s, snapshot)) is not None]
        score = round(sum(f.weight for f in fired), 3)
        out.append(Detection(plugin.id, score, plugin.detectors.min_score, tuple(fired)))
    return sorted(out, key=lambda d: (not d.detected, -d.score, d.plugin))


def _fire(signal: Signal, snap: SourceSnapshot) -> Fired | None:  # noqa: PLR0911 — one branch per kind
    if signal.files is not None:
        return _files(signal, snap)
    if signal.incidents is not None:
        return _incidents(signal, snap)
    if signal.docs is not None:
        pattern = re.compile(signal.docs, re.IGNORECASE)
        pages = [p for p in snap.docs if p.current and pattern.search(p.text)]
        if len(pages) < signal.min_pages:
            return None
        shown = ", ".join(p.title for p in pages[:_EVIDENCE_SHOWN])
        return Fired("docs", signal.weight, f"{len(pages)} current page(s) mention it ({shown})")
    if signal.cmdb_class is not None:
        if signal.cmdb_class not in snap.cmdb_classes:
            return None
        return Fired("cmdb_class", signal.weight, f"CMDB has an installed {signal.cmdb_class}")
    if signal.git_host is not None:
        if signal.git_host not in snap.git_hosts:
            return None
        return Fired("git_host", signal.weight, f"the code is on {signal.git_host}")
    if signal.changes_caused is not None and snap.incidents:
        caused = sum(i.caused_by_change for i in snap.incidents)
        share = caused / len(snap.incidents)
        if share < signal.changes_caused:
            return None
        return Fired(
            "changes_caused",
            signal.weight,
            f"{caused} of {len(snap.incidents)} incidents were caused by a change",
        )
    return None


def _files(signal: Signal, snap: SourceSnapshot) -> Fired | None:
    assert signal.files is not None  # noqa: S101 — the caller checked the kind
    pattern = re.compile(signal.contains) if signal.contains else None
    hits = sorted(
        path
        for path, text in snap.files.items()
        if _path_matches(path, signal.files) and (pattern is None or pattern.search(text))
    )
    if not hits:
        return None
    what = f"contain {signal.contains!r}" if signal.contains else "exist"
    shown = ", ".join(hits[:_EVIDENCE_SHOWN]) + (" ..." if len(hits) > _EVIDENCE_SHOWN else "")
    return Fired(
        "files", signal.weight, f"{len(hits)} file(s) matching {signal.files} {what}: {shown}"
    )


def _incidents(signal: Signal, snap: SourceSnapshot) -> Fired | None:
    assert signal.incidents is not None  # noqa: S101 — the caller checked the kind
    if not snap.incidents:
        return None
    pattern = re.compile(signal.incidents, re.IGNORECASE)
    hits = [i for i in snap.incidents if pattern.search(i.text)]
    share = len(hits) / len(snap.incidents)
    if share < signal.min_share:
        return None
    return Fired(
        "incidents",
        signal.weight,
        f"{len(hits)} of {len(snap.incidents)} incidents match /{signal.incidents}/",
    )


def _path_matches(path: str, glob: str) -> bool:
    """``fnmatch`` where ``**/`` also matches zero directories."""
    if fnmatch.fnmatchcase(path, glob):
        return True
    return glob.startswith("**/") and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], glob[3:])


def detected_ids(detections: Sequence[Detection]) -> list[str]:
    return [d.plugin for d in detections if d.detected]


__all__ = [
    "NOT_CURRENT_LABELS",
    "Detection",
    "DocPage",
    "Fired",
    "IncidentText",
    "SourceSnapshot",
    "detect",
    "detected_ids",
]
