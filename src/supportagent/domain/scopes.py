"""Literal grant/proposal scopes (ARCH §6.4, ADR-0005).

Scopes are literal strings compared with ``=`` by the gate trigger; there is
no pattern matching in the enforcement path. Two shapes exist:

- **T1 kinds** (``work_note``, ``enrich_page``, ``watch_list_add_internal``)
  are scoped to the catalog service: ``service:<name>:<kind>``. One approved
  setup grant then covers that kind on every case of that service, while the
  action's ``TargetRef`` still names the concrete record (ADR-0005).
- **Every other kind** is scoped to its concrete target, e.g.
  ``servicenow:incident:<sys_id>:ticket_update`` or ``airflow:dag:nightly-sync``.

This module is the one place both shapes are built, so a proposal and the
setup grant meant to cover it cannot drift apart.
"""

from __future__ import annotations

from supportagent.domain.kinds import KIND_META, ActionKind
from supportagent.domain.tiers import Tier

#: The default T1 kinds requested at first-run setup (ARCH §6.4).
DEFAULT_T1_KINDS: tuple[ActionKind, ...] = tuple(
    kind for kind, meta in KIND_META.items() if meta.tier is Tier.T1
)


def is_service_scoped(kind: ActionKind) -> bool:
    """True for the kinds whose scope is the catalog service (T1, ADR-0005)."""
    return KIND_META[kind].tier is Tier.T1


def service_scope(service: str, kind: ActionKind) -> str:
    """``service:<name>:<kind>`` — the literal scope of a T1 kind (ADR-0005)."""
    return f"service:{service}:{kind.value}"


def record_scope(system: str, table: str, ref: str, kind: ActionKind) -> str:
    """``<system>:<table>:<ref>:<kind>`` — the literal scope of a per-target kind."""
    return f"{system}:{table}:{ref}:{kind.value}"
