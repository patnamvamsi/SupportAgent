"""Per-connector doctor checks (WP-M07).

Each connector registers a :class:`~supportagent.app.doctor.base.ConnectorDoctor`
here: its own checks (auth, note visibility, roles, rate limit, history
access, audit retention for ServiceNow) and how it discovers automation
actors. This package is where connector-specific diagnostics live; the rest
of the core never names a connector (``test_core_has_no_servicenow_types``).
"""

from __future__ import annotations

from collections.abc import Mapping

from supportagent.app.doctor.base import ConnectorDoctor
from supportagent.app.doctor.connectors.servicenow import SERVICENOW_DOCTOR
from supportagent.domain.errors import ConfigError

CONNECTOR_DOCTORS: Mapping[str, ConnectorDoctor] = {SERVICENOW_DOCTOR.name: SERVICENOW_DOCTOR}


def connector_doctor(name: str) -> ConnectorDoctor:
    try:
        return CONNECTOR_DOCTORS[name]
    except KeyError:
        known = ", ".join(sorted(CONNECTOR_DOCTORS))
        raise ConfigError(f"no doctor checks for connector {name!r} (known: {known})") from None  # noqa: TRY003


__all__ = ["CONNECTOR_DOCTORS", "connector_doctor"]
