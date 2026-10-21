"""Local checks (02 §4.1): ``config``, ``db``, ``paging``."""

from __future__ import annotations

from supportagent.app.doctor.base import CheckResult, DoctorContext
from supportagent.config.validation import validate_bundle
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.calibration import CalibrationTable


class ConfigCheck:
    """All files validate; taxonomy references resolve; calibration grade ≥ ``min_grade``."""

    name = "config"

    def run(self, ctx: DoctorContext) -> CheckResult:
        problems: list[str] = []
        try:
            bundle = ctx.load_bundle()
        except ConfigError as exc:
            return CheckResult(self.name, "fail", str(exc))
        problems += [
            f"{i.path}: {i.message}"
            for i in validate_bundle(ctx.config, bundle.catalog, bundle.taxonomy, bundle.hypotheses)
        ]
        table: CalibrationTable | None = None
        try:
            table = ctx.load_calibration()
        except ConfigError as exc:
            problems.append(str(exc))
        if problems or table is None:
            return CheckResult(
                self.name, "fail", f"{len(problems)} issue(s): " + "; ".join(problems)
            )
        return CheckResult(
            self.name,
            "pass",
            f"config, catalog ({len(bundle.catalog.services)} services), taxonomies and "
            f"calibration (grade {table.grade}, {table.model_id}) validate",
        )


class DbCheck:
    """Connects; migrations at head; ``PRAGMA foreign_keys`` = 1 on a fresh connection."""

    name = "db"

    def run(self, ctx: DoctorContext) -> CheckResult:
        current = ctx.schema.current_revision()
        head = ctx.schema.head_revision()
        if current != head:
            return CheckResult(
                self.name,
                "fail",
                f"schema at {current or 'nothing'}, build expects {head}: "
                "run `supportagent migrate`",
            )
        if ctx.schema.foreign_keys_enforced() is False:
            return CheckResult(
                self.name, "fail", "SQLite foreign_keys is off on a fresh connection"
            )
        return CheckResult(self.name, "pass", f"connected; schema at head ({head})")


class PagingCheck:
    """Every catalog service has a ``paging_integration`` (ARCH §14), with a provider.

    No ``paging_integration`` fails: triage is refused for that service
    (ADR-0006). One whose provider isn't configured under ``paging:`` warns:
    the Null Object answers (no lookups, ``enrich_page`` a no-op; WP-112).
    """

    name = "paging"

    def run(self, ctx: DoctorContext) -> CheckResult:
        try:
            catalog = ctx.load_bundle().catalog
        except ConfigError as exc:
            return CheckResult(self.name, "fail", f"catalog not loadable: {exc}")
        missing = catalog.unpaged_services()
        if missing:
            return CheckResult(
                self.name,
                "fail",
                "no paging_integration for "
                + ", ".join(missing)
                + ": triage is refused for these services (ARCH §14)",
                {"unpaged": missing},
            )
        # WP-112: a real provider replaces the Null Object for each service.
        paging = ctx.config.paging
        configured = {
            name
            for name, cfg in (("pagerduty", paging.pagerduty), ("opsgenie", paging.opsgenie))
            if cfg is not None
        }
        null = sorted(
            s.name
            for s in catalog.services
            if (s.paging_integration or "").split(":", 1)[0] not in configured
        )
        if null:
            return CheckResult(
                self.name,
                "warn",
                "no paging provider configured for "
                + ", ".join(null)
                + ": enrich_page is a no-op and no page_recommended is raised for them "
                "(configure paging.pagerduty / paging.opsgenie, ARCH §14)",
                {"null_paging": null},
            )
        return CheckResult(
            self.name,
            "pass",
            f"all {len(catalog.services)} services have a configured paging integration",
        )


__all__ = ["ConfigCheck", "DbCheck", "PagingCheck"]
