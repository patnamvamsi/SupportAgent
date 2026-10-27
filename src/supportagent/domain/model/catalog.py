"""Service catalog: ``ServiceEntry`` and ``Catalog`` with case-service resolution.

01 §4 fixes the field set; ARCH §7 fixes the resolution order — an alert's
``resource`` maps to ``ServiceEntry.resources`` first, then its CMDB CI to
``ServiceEntry.cmdb_ci``; otherwise the case safe-defaults.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from supportagent.domain.model.base import DomainModel


class ServiceEntry(DomainModel):
    """One service row of ``catalog/services.yaml`` (01 §4)."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    repos: list[str] = Field(default_factory=list)
    log_paths: list[str] = Field(default_factory=list)
    dags: list[str] = Field(default_factory=list)
    cmdb_ci: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    oncall_schedule: str | None = None
    owners: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    paging_integration: str | None = None
    #: ADR-0011: technology tags that select skill packs (``python``, ``postgres``, …).
    stack: list[str] = Field(default_factory=list)
    #: WP-M04: the ``tools.postgres.databases`` this service may read (the allowlist).
    databases: list[str] = Field(default_factory=list)
    #: WP-M04: where its pods run, for ``k8s.pod_status`` and rollouts.
    k8s_namespace: str | None = None
    k8s_selector: str | None = None
    #: WP-M08: the knowledge spaces this service's docs live in.
    docs: list[str] = Field(default_factory=list)


class Catalog(DomainModel):
    """Whole ``catalog/services.yaml`` file (01 §4, ARCH §7).

    ``version`` is free-form and customer-owned; it is recorded in
    ``decision_json.catalog_version`` (06 §3).
    """

    version: str = Field(min_length=1)
    services: list[ServiceEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names_and_known_dependencies(self) -> Catalog:
        names = [service.name for service in self.services]
        if len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise ValueError("services names must be unique; duplicates: " + ", ".join(duplicates))
        known = set(names)
        for service in self.services:
            for dependency in service.depends_on:
                if dependency not in known:
                    raise ValueError(  # noqa: TRY003
                        f"service '{service.name}' depends_on unknown service '{dependency}'"
                    )
        return self

    def resolve_service(
        self,
        *,
        resource: str | None = None,
        cmdb_ci: str | None = None,
    ) -> str | None:
        """Resolve the case's service name (ARCH §7); ``None`` when unresolvable."""
        if resource is not None:
            for service in self.services:
                if resource in service.resources:
                    return service.name
        if cmdb_ci is not None:
            for service in self.services:
                if cmdb_ci in service.cmdb_ci:
                    return service.name
        return None

    def service(self, name: str) -> ServiceEntry | None:
        """The catalog row for ``name``, or ``None``."""
        return next((s for s in self.services if s.name == name), None)

    def unpaged_services(self) -> list[str]:
        """Services with no ``paging_integration`` (ARCH §14: triage is refused for them)."""
        return [s.name for s in self.services if not s.paging_integration]
