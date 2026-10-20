"""Comms plan templates and message templates (11-wp WP-102).

``templates/comms/plans.yaml`` defines the plan templates a routing row's
``comms:`` names; ``templates/comms/<template_id>.txt`` are the message
bodies. Every plan is checked at load: valid sources, and a template whose
placeholders are all allowed fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.comms import AllowedField, PlanSpec, check_template
from supportagent.domain.errors import ConfigError, DomainValidationError


@dataclass(frozen=True, slots=True)
class CommsCatalog:
    plans: dict[str, PlanSpec]
    templates: dict[str, str]


def load_comms_catalog(directory: Path) -> CommsCatalog:
    data = load_yaml_file(directory / "plans.yaml")
    raw = data.get("plans", {})
    if not isinstance(raw, dict):
        raise ConfigError(f"{directory}/plans.yaml: 'plans' must be a mapping")  # noqa: TRY003
    plans: dict[str, PlanSpec] = {}
    templates: dict[str, str] = {}
    for plan_id, body in raw.items():
        where = f"{directory}/plans.yaml: {plan_id}"
        try:
            fields = body.get("allowed_fields", {})
            spec = PlanSpec(
                audience=body["audience"],
                channel=body["channel"],
                template_id=body["template_id"],
                allowed_fields=tuple(AllowedField(n, s) for n, s in sorted(fields.items())),
                cadence_minutes=body.get("cadence_minutes"),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ConfigError(f"{where}: missing or malformed {exc}") from exc  # noqa: TRY003
        except DomainValidationError as exc:
            raise ConfigError(f"{where}: {exc}") from exc  # noqa: TRY003
        path = directory / f"{spec.template_id}.txt"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{where}: template {path} unreadable: {exc}") from exc  # noqa: TRY003
        try:
            check_template(text, spec.fields)
        except DomainValidationError as exc:
            raise ConfigError(f"{where}: {exc}") from exc  # noqa: TRY003
        plans[str(plan_id)] = spec
        templates[spec.template_id] = text
    return CommsCatalog(plans=plans, templates=templates)


__all__ = ["CommsCatalog", "load_comms_catalog"]
