"""Routing follows readiness (ADR-0015 §5, WP-O6b).

Once a system has a readiness report, only what its history proved routes:

- **Triage:** a case whose label no *ready* class carries safe-defaults to a
  person (``class-not-ready``). Labels match exactly: a ready
  ``database/latency`` class says nothing about ``database/failure``.
- **RCA:** a claim the tools support for a cause whose class isn't ready is
  tagged ``not_ready``, which makes it unsendable. One label can hold a ready
  cause and one that isn't (lock contention and pool exhaustion are both
  ``database/saturation`` in System A).

A system without a report routes as before: readiness gates what onboarding
measured, it doesn't invent a gate where nothing was measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: The tag on a claim whose cause belongs to a class that isn't ready.
NOT_READY_TAG = "not_ready"


@dataclass(frozen=True, slots=True)
class ReadinessPolicy:
    ready_labels: frozenset[str]
    #: Hypotheses of the classes that aren't ready (untestable or failing a gate).
    unproven: frozenset[str]

    def routes(self, label: str | None) -> bool:
        return label is not None and label in self.ready_labels

    def proven(self, hypothesis: str) -> bool:
        return hypothesis not in self.unproven

    @classmethod
    def of(cls, classes: Iterable[Mapping[str, object]]) -> ReadinessPolicy:
        """From a report's ``classes`` (``evals.readiness.ReadinessReport.as_dict``)."""
        ready: set[str] = set()
        unproven: set[str] = set()
        for c in classes:
            label, hypothesis = c.get("label"), c.get("hypothesis")
            if c.get("status") == "ready":
                if isinstance(label, str):
                    ready.add(label)
            elif isinstance(hypothesis, str):
                unproven.add(hypothesis)
        return cls(frozenset(ready), frozenset(unproven))


__all__ = ["NOT_READY_TAG", "ReadinessPolicy"]
