"""WP-002: service catalog model + resolution (01 §4, ARCH §7).

Canonical test IDs from 10-wp §WP-002:
``test_catalog_resolve_by_resource``, ``test_catalog_resolve_by_cmdb_ci``,
``test_catalog_unresolved_returns_none``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from supportagent.domain.model import Catalog
from tests.builders import a_catalog_file


def test_catalog_resolve_by_resource() -> None:
    # ARCH §7: an alert's resource maps to ServiceEntry.resources.
    catalog = Catalog(**a_catalog_file())
    assert catalog.resolve_service(resource="db-pool-01") == "nightly-sync"
    assert catalog.resolve_service(resource="svc:payroll-api") == "payroll-api"


def test_catalog_resolve_by_cmdb_ci() -> None:
    # ARCH §7: then the CMDB CI maps to ServiceEntry.cmdb_ci.
    catalog = Catalog(**a_catalog_file())
    assert catalog.resolve_service(cmdb_ci="CI3003") == "payroll-api"
    assert catalog.resolve_service(cmdb_ci="CI2002") == "core-db"


def test_catalog_unresolved_returns_none() -> None:
    # ARCH §7: otherwise the case safe-defaults — resolution returns None.
    catalog = Catalog(**a_catalog_file())
    assert catalog.resolve_service(resource="no-such-resource") is None
    assert catalog.resolve_service(cmdb_ci="CI9999") is None
    assert catalog.resolve_service() is None


def test_catalog_rejects_unknown_service_in_depends_on() -> None:
    # 01 §4: depends_on references other services — dangling refs are a
    # validation error at load time (the bundle check reports cross-file).
    data = a_catalog_file()
    data["services"][0]["depends_on"] = ["ghost-service"]
    with pytest.raises(ValidationError, match="ghost-service"):
        Catalog(**data)
