"""BL-030: ServiceNow reference fields map to bare sys_ids."""

from __future__ import annotations

from supportagent.adapters.servicenow.mapping import row_to_record


def test_mapping_unwraps_a_reference_object() -> None:
    """BL-030: a ``{"link", "value"}`` reference still maps to its sys_id."""
    row = {
        "sys_id": "s1",
        "cmdb_ci": {"link": "https://x.service-now.com/api/now/table/cmdb_ci/ci1", "value": "ci1"},
        "caller_id": {"link": "", "value": ""},
        "sys_updated_on": "2026-05-21 20:48:46",
        "sys_mod_count": "2",
    }
    rec = row_to_record("incident", row)
    assert rec.cmdb_ci == "ci1"
    assert rec.caller_id is None
