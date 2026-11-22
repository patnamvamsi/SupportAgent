"""The service catalog onboarding drafts, reviewed in the instance (ADR-0023 §2, WP-223).

The ``catalog`` onboarding step drafts a service entry for every component of
the graph under review (``draft_catalog``) and stores the drafts here. A person
accepts the ones the instance should know on ``/ui/onboarding/catalog``. Each
accepted entry needs a ``paging_integration``, because triage refuses a service
without one (ARCH §14). Onboarding can't know a team's pager, so the person
gives it, ``none:<who>`` included.

Accepted services join the configured catalog (the file keeps any name it has)
**without a restart**: the long-lived services read a :class:`LiveCatalog`,
which reloads both at most every :data:`RELOAD_S` seconds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from supportagent.app.readiness_store import TenantDocument
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.ports.clock import Clock

RELOAD_S = 30
_log = logging.getLogger(__name__)


class CatalogReviewStore(TenantDocument):
    table, column = "catalog_review", "review_json"


class CatalogReview:
    def __init__(self, store: CatalogReviewStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def state(self) -> dict[str, Any]:
        return self._store.load() or {}

    def save_drafts(self, drafts: Sequence[tuple[ServiceEntry, str, Sequence[str]]]) -> None:
        """The step's drafts: ``(entry, new|kept, what's left)``. Acceptances are kept."""
        self._store.save(
            self.state()
            | {
                "drafted_ts": self._now(),
                "drafts": [
                    {
                        "entry": entry.model_dump(mode="json", exclude_defaults=True),
                        "status": status,
                        "todo": list(todo),
                    }
                    for entry, status, todo in drafts
                ],
            }
        )

    def accept(self, paging: Mapping[str, str], *, actor: str) -> int:
        """Accept drafted services by name, each with its pager; returns how many."""
        state = self.state()
        drafted = {d["entry"]["name"]: d for d in state.get("drafts", [])}
        unknown = sorted(set(paging) - set(drafted))
        if unknown:
            raise DomainValidationError(f"no drafted service: {', '.join(unknown)}")  # noqa: TRY003
        missing = sorted(n for n, p in paging.items() if not p.strip())
        if missing:
            raise DomainValidationError(  # noqa: TRY003
                "each accepted service needs a paging integration (pagerduty:<service>, "
                f"opsgenie:<team> or none:<who>): {', '.join(missing)}"
            )
        accepted: dict[str, Any] = dict(state.get("accepted", {}))
        now = self._now()
        for name, pager in paging.items():
            raw = drafted[name]["entry"] | {"paging_integration": pager.strip()}
            try:
                ServiceEntry.model_validate(raw)
            except ValidationError as exc:
                detail = exc.errors()[0]["msg"]
                raise DomainValidationError(f"{name}: {detail}") from exc  # noqa: TRY003
            accepted[name] = {"entry": raw, "by": actor, "ts": now}
        self._store.save(state | {"accepted": accepted, "reviewed_ts": now})
        return len(paging)

    def withdraw(self, names: Sequence[str], *, actor: str) -> int:
        state = self.state()
        accepted: dict[str, Any] = dict(state.get("accepted", {}))
        gone = [n for n in names if accepted.pop(n, None) is not None]
        self._store.save(state | {"accepted": accepted, "reviewed_ts": self._now()})
        return len(gone)

    def accepted(self) -> list[ServiceEntry]:
        return [
            ServiceEntry.model_validate(a["entry"])
            for _, a in sorted(self.state().get("accepted", {}).items())
        ]

    def unreviewed(self) -> list[str]:
        """Drafted new services nobody has accepted yet."""
        state = self.state()
        accepted = state.get("accepted", {})
        return sorted(
            d["entry"]["name"]
            for d in state.get("drafts", [])
            if d["status"] == "new" and d["entry"]["name"] not in accepted
        )

    def _now(self) -> int:
        return int(self._clock.now().timestamp())


class LiveCatalog:
    """A ``CatalogView`` that follows accepted services: reloaded at most every ``reload_s``.

    A reload that fails (the file unreadable, the database down) keeps the last
    catalog: reads never fail because a refresh did.
    """

    def __init__(
        self, load: Callable[[], Catalog], *, clock: Clock, reload_s: int = RELOAD_S
    ) -> None:
        self._load = load
        self._clock = clock
        self._reload_s = reload_s
        self._catalog = load()
        self._at = self._clock.now().timestamp()

    def current(self) -> Catalog:
        now = self._clock.now().timestamp()
        if now - self._at >= self._reload_s:
            self._at = now
            try:
                self._catalog = self._load()
            except Exception:  # noqa: BLE001 — keep serving the last good catalog
                _log.warning("catalog reload failed; keeping the last one", exc_info=True)
        return self._catalog

    @property
    def version(self) -> str:
        return self.current().version

    @property
    def services(self) -> list[ServiceEntry]:
        return self.current().services

    def resolve_service(
        self, *, resource: str | None = None, cmdb_ci: str | None = None
    ) -> str | None:
        return self.current().resolve_service(resource=resource, cmdb_ci=cmdb_ci)

    def service(self, name: str) -> ServiceEntry | None:
        return self.current().service(name)

    def unpaged_services(self) -> list[str]:
        return self.current().unpaged_services()


__all__ = ["RELOAD_S", "CatalogReview", "CatalogReviewStore", "LiveCatalog"]
