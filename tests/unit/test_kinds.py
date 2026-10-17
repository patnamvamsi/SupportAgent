"""WP-001: ActionKind + KIND_META, canonical against ARCH §12.2."""

from __future__ import annotations

from pathlib import Path

from supportagent.domain.kinds import (
    CUSTOMER_FACING_KINDS,
    KIND_META,
    NEVER_EXECUTABLE_KINDS,
    ActionKind,
    KindMeta,
)
from supportagent.domain.tiers import Tier

ARCH = Path(__file__).resolve().parents[2] / "docs" / "spec" / "ARCHITECTURE.md"


def _clean(cell: str) -> str:
    """Strip markdown emphasis/code markers from a table cell (values, not styling)."""
    return cell.replace("**", "").replace("`", "").strip()


def _parse_arch_table() -> dict[str, KindMeta]:
    text = ARCH.read_text(encoding="utf-8")
    section = text.split("### 12.2", 1)[1].split("\n---", 1)[0]
    rows: dict[str, KindMeta] = {}
    for line in section.splitlines():
        if not line.lstrip().startswith("| `"):
            continue
        cells = [_clean(cell) for cell in line.strip().strip("|").split("|")]
        rows[cells[0]] = KindMeta(
            tier=Tier(cells[1]) if cells[1] != "—" else None,
            audience=cells[2],
            paths=cells[3],
        )
    return rows


def test_kind_meta_matches_arch_table() -> None:
    expected = _parse_arch_table()
    assert len(expected) == 12
    assert set(KIND_META) == set(expected)
    assert dict(KIND_META) == expected
    # Every ActionKind is covered, including the never-executable one.
    assert ActionKind.CREATE_PAGE in KIND_META
    assert KIND_META[ActionKind.CREATE_PAGE].tier is None
    assert KIND_META[ActionKind.CREATE_PAGE].paths == "never executable in v0.x"


def test_customer_facing_and_never_executable_sets() -> None:
    table = _parse_arch_table()
    customer_from_table = {
        ActionKind(kind) for kind, meta in table.items() if meta.audience == "customer"
    }
    assert customer_from_table == CUSTOMER_FACING_KINDS
    assert {
        ActionKind.WATCH_LIST_ADD_CUSTOMER,
        ActionKind.CUSTOMER_COMMENT,
        ActionKind.SEND_EMAIL_CUSTOMER,
        ActionKind.CREATE_TICKET_CUSTOMER,
    } == CUSTOMER_FACING_KINDS
    assert {ActionKind.CREATE_PAGE} == NEVER_EXECUTABLE_KINDS
    # ARCH §12.2: customer-facing kinds are approval-only, never grant-authorized, all T2.
    assert all(KIND_META[kind].tier is Tier.T2 for kind in CUSTOMER_FACING_KINDS)
    assert all("approval" in KIND_META[kind].paths for kind in CUSTOMER_FACING_KINDS)
    assert not CUSTOMER_FACING_KINDS & NEVER_EXECUTABLE_KINDS
