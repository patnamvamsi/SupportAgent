"""The doctor's contract (02 §4.1, 10-wp WP-015).

A ``Check`` is one row of the 02 §4.1 table: ``run(ctx) -> CheckResult``
with status ``pass`` / ``warn`` / ``fail`` and a one-line detail. The
``Doctor`` runs a registry of checks in order. A check that raises becomes a
``fail`` with the error's message, so one broken dependency never hides the
others. Exit codes (02 §4): 0 all pass, 1 any fail, 3 warnings only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from supportagent.config.schema import AppConfig
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.taxonomy import ClassificationTaxonomy, HypothesisTaxonomy
from supportagent.ports.clock import Clock
from supportagent.ports.diagnostics import (
    AutomationActorStore,
    DiscoveryProbe,
    ModelProbe,
    SchemaProbe,
    ServiceNowProbe,
)

CheckStatus = Literal["pass", "warn", "fail"]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_WARN = 3


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    data: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            **({"data": dict(self.data)} if self.data else {}),
        }


@dataclass(frozen=True, slots=True)
class Bundle:
    """The config's companion files (02 §1 ``paths``)."""

    catalog: Catalog
    taxonomy: ClassificationTaxonomy
    hypotheses: HypothesisTaxonomy


@dataclass(frozen=True, slots=True)
class DoctorContext:
    """Everything a check may read. Loaders raise ``ConfigError``; probes raise typed errors."""

    config: AppConfig
    load_bundle: Callable[[], Bundle]
    load_calibration: Callable[[], CalibrationTable]
    schema: SchemaProbe
    servicenow: ServiceNowProbe
    llm: ModelProbe
    identity: DiscoveryProbe
    automation: AutomationActorStore
    clock: Clock
    discover_automation: bool = False


class Check(Protocol):
    name: str

    def run(self, ctx: DoctorContext) -> CheckResult: ...


@dataclass(frozen=True, slots=True)
class DoctorReport:
    results: list[CheckResult]

    @property
    def exit_code(self) -> int:
        statuses = {r.status for r in self.results}
        if "fail" in statuses:
            return EXIT_FAIL
        if "warn" in statuses:
            return EXIT_WARN
        return EXIT_OK

    def as_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "checks": [r.as_dict() for r in self.results],
        }


class Doctor:
    """Runs the registry in order; never stops at the first failure."""

    def __init__(self, checks: Sequence[Check]) -> None:
        self._checks = list(checks)

    def run(self, ctx: DoctorContext) -> DoctorReport:
        return DoctorReport(results=[self._run(check, ctx) for check in self._checks])

    @staticmethod
    def _run(check: Check, ctx: DoctorContext) -> CheckResult:
        try:
            return check.run(ctx)
        except Exception as exc:  # noqa: BLE001 — a broken dependency is that check's fail
            return CheckResult(check.name, "fail", f"{type(exc).__name__}: {exc}")


__all__ = [
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_WARN",
    "Bundle",
    "Check",
    "CheckResult",
    "CheckStatus",
    "Doctor",
    "DoctorContext",
    "DoctorReport",
]
