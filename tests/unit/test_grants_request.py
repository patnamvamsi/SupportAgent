"""WP-012: ``grants request --from config`` expands patterns via the catalog.

Canonical test ID from 10-wp §WP-012: ``test_grants_request_expands_pattern_to_literals``.
ARCH §6.4: scopes are literal; a pattern is expanded at request time into one
literal row per concrete catalog target.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.setup import SetupService, expand_grant_requests
from supportagent.config.schema import GrantRequest
from supportagent.domain.errors import ConfigError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.catalog import Catalog, ServiceEntry


def _catalog() -> Catalog:
    return Catalog(
        version="1",
        services=[
            ServiceEntry(
                name="batch",
                repos=["acme/batch", "acme/batch-tools"],
                log_paths=[],
                dags=["nightly-sync", "hourly-agg"],
                cmdb_ci=[],
                resources=[],
                owners=["team-batch"],
                depends_on=[],
            ),
            ServiceEntry(
                name="web",
                repos=["acme/web"],
                log_paths=[],
                dags=["nightly-report"],
                cmdb_ci=[],
                resources=[],
                owners=["team-web"],
                depends_on=[],
            ),
        ],
    )


def _req(action: str, system: str, target: str) -> GrantRequest:
    return GrantRequest(action=action, system=system, target=target, requested_expiry_days=90)


def test_grants_request_expands_pattern_to_literals() -> None:
    requests = [
        _req("job_rerun", "airflow", "dag:nightly-*"),
        _req("job_rerun", "gha", "repo:acme/batch*:workflow:deploy.yml:rerun_failed_only"),
        _req("job_rerun", "airflow", "dag:hourly-agg"),  # literal: passes through
    ]
    literals = expand_grant_requests(requests, _catalog())
    assert literals == [
        (ActionKind.JOB_RERUN, "airflow:dag:nightly-report"),
        (ActionKind.JOB_RERUN, "airflow:dag:nightly-sync"),
        (ActionKind.JOB_RERUN, "gha:repo:acme/batch-tools:workflow:deploy.yml:rerun_failed_only"),
        (ActionKind.JOB_RERUN, "gha:repo:acme/batch:workflow:deploy.yml:rerun_failed_only"),
        (ActionKind.JOB_RERUN, "airflow:dag:hourly-agg"),
    ]
    assert not any("*" in scope for _, scope in literals), "no pattern reaches the DB"


@pytest.mark.parametrize(
    ("request_", "match"),
    [
        (_req("job_rerun", "airflow", "dag:missing-*"), "matches no catalog"),
        (_req("job_rerun", "airflow", "nope:*"), "cannot expand"),
        (_req("not_a_kind", "airflow", "dag:nightly-sync"), "unknown action"),
        (_req("job_rerun", "airflow", "dag:*:x*"), "only the target name"),
    ],
    ids=["no-match", "unknown-field", "unknown-kind", "glob-outside-name"],
)
def test_grants_request_rejects_bad_requests(request_: GrantRequest, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        expand_grant_requests([request_], _catalog())


def test_grants_request_writes_requested_rows(db: Any, clock: Any, ids: Any, events: Any) -> None:
    setup = SetupService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        catalog=_catalog(),
        tenant_id="acme",
    )
    literals = expand_grant_requests([_req("job_rerun", "airflow", "dag:nightly-*")], _catalog())
    created = setup.request_grants(literals, requested_by="config:abc")
    assert len(created) == 2
    assert setup.request_grants(literals, requested_by="config:abc") == []
    with db.connect() as conn:
        rows = conn.execute(text("SELECT scope, status FROM policy_grants ORDER BY scope")).all()
    assert [tuple(r) for r in rows] == [
        ("airflow:dag:nightly-report", "requested"),
        ("airflow:dag:nightly-sync", "requested"),
    ]
