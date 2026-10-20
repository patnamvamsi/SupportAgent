"""``supportagent doctor`` (02 §4.1, 10-wp WP-015): the check registry and runner."""

from supportagent.app.doctor.base import (
    Bundle,
    Check,
    CheckResult,
    Doctor,
    DoctorContext,
    DoctorReport,
)
from supportagent.app.doctor.checks import default_checks

__all__ = [
    "Bundle",
    "Check",
    "CheckResult",
    "Doctor",
    "DoctorContext",
    "DoctorReport",
    "default_checks",
]
