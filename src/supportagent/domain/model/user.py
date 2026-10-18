"""User: a directory identity (01 §3: id is the stable OIDC sub)."""

from __future__ import annotations

from supportagent.domain.model.base import DomainModel


class User(DomainModel):
    """A users row; id is the OIDC sub (01 §3, ARCH §6.4)."""

    id: str
    tenant_id: str
    name: str
    email: str
    is_active: bool
