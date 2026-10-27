"""ServiceNow's connector error (01 §6, WP-M07)."""

from __future__ import annotations

from supportagent.domain.errors import ConnectorError


class ServiceNowError(ConnectorError):
    """A ServiceNow Table API call failed. ``status`` is the HTTP status, when there was one."""


__all__ = ["ServiceNowError"]
