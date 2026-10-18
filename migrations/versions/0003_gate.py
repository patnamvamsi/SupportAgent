"""INV-01 human gate: proposals, approvals, policy_grants, comms_plans, outbox.

Adds the 10-wp gate tables (03 §4) and the two enforcement points that make
INV-01 a *system invariant*, not a caller convention (01 §3, 06 §2):

* ``trg_outbox_inv01`` (BEFORE INSERT ON outbox) — an outbox row may exist
  only if it is backed by an *enforceable* authorization: an approved
  proposal+approval with matching payload hash and action kind, an approved
  non-customer-facing policy grant that is in scope and unexpired, or an
  approved comms plan. ``create_page`` is never executable, full stop.
* ``trg_no_self_approval`` (BEFORE INSERT ON approvals) — an approver may
  not approve their own proposal.
* ``trg_outbox_mark_executed`` (AFTER INSERT ON outbox) — executing a proposal
  transitions it to ``executed`` (single-use).
* ``trg_outbox_immutable`` — the authorization/payload columns of an outbox row
  are immutable after insert (INV-01 §3).

The two kind lists are generated from the domain enums (``NEVER_EXECUTABLE_KINDS``,
``CUSTOMER_FACING_KINDS``) so ``test_trigger_kind_lists_match_code`` stays green
if the domain set changes. Dialect branches: SQLite inline triggers, Postgres
PL/pgSQL functions (03 §4).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Domain enum → SQL literal sets (03 §1: CHECK literals generated from enums)
# ---------------------------------------------------------------------------

from supportagent.domain.kinds import (  # noqa: E402  (import after revision block, alembic style)
    CUSTOMER_FACING_KINDS,
    NEVER_EXECUTABLE_KINDS,
    ActionKind,
)


def _ordered(values: frozenset[ActionKind]) -> list[str]:
    """Deterministic ordering by the ActionKind definition order."""
    return [k.value for k in ActionKind if k in values]


NEVER: list[str] = _ordered(NEVER_EXECUTABLE_KINDS)
CUSTOMER: list[str] = _ordered(CUSTOMER_FACING_KINDS)


def _lit_list(values: list[str]) -> str:
    return "(" + ", ".join(repr(v) for v in values) + ")"


PROPOSAL_STATUSES = (
    "proposed",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "executed",
    "superseded",
)


def _gate_condition(prefix: str, now_expr: str) -> str:
    """The single INV-01 predicate: an outbox row is enforceable iff it is
    backed by a valid approval, a valid (non-customer) grant, or a valid
    comms plan — and ``create_page`` is never executable."""
    kind = f"{prefix}.action_kind"
    tenant = f"{prefix}.tenant_id"
    return (
        f"{kind} IN {_lit_list(NEVER)} "
        "OR NOT ("
        # Path 1: an approved proposal + approval with matching hash and kind.
        f"({prefix}.approval_id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM approvals a JOIN proposals p ON p.id = a.proposal_id "
        f"WHERE a.id = {prefix}.approval_id "
        "AND a.status = 'approved' AND p.status = 'approved' "
        f"AND a.tenant_id = {tenant} AND p.tenant_id = {tenant} "
        f"AND a.payload_hash = {prefix}.payload_hash "
        f"AND p.payload_hash = {prefix}.payload_hash "
        f"AND p.action_kind = {kind}"
        ")) "
        "OR "
        # Path 2: an approved policy grant — never for customer-facing kinds,
        # in scope, and unexpired.
        f"({prefix}.policy_grant_id IS NOT NULL "
        f"AND {kind} NOT IN {_lit_list(CUSTOMER)} "
        "AND EXISTS ("
        "SELECT 1 FROM policy_grants g "
        f"WHERE g.id = {prefix}.policy_grant_id "
        "AND g.status = 'approved' "
        f"AND g.tenant_id = {tenant} "
        f"AND g.action_kind = {kind} AND g.scope = {prefix}.scope "
        f"AND g.valid_to_ts > {now_expr}"
        ")) "
        "OR "
        # Path 3: an approved comms plan backed by an approved approval.
        f"({prefix}.comms_plan_id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM comms_plans cp "
        f"WHERE cp.id = {prefix}.comms_plan_id "
        "AND cp.status = 'approved' "
        f"AND cp.tenant_id = {tenant} "
        "AND EXISTS ("
        "SELECT 1 FROM approvals a "
        "WHERE a.id = cp.approval_id AND a.status = 'approved' "
        f"AND a.tenant_id = {tenant}"
        "))"
        "))"
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # --- proposals ----------------------------------------------------------
    op.create_table(
        "proposals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("action_kind", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at_ts", sa.BigInteger, nullable=False),
        sa.Column("expires_ts", sa.BigInteger, nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in PROPOSAL_STATUSES)})",
            name="ck_proposals_status",
        ),
    )
    op.create_index("ix_proposals_case", "proposals", ["tenant_id", "case_id"])

    # --- approvals ----------------------------------------------------------
    op.create_table(
        "approvals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("proposal_id", sa.Text, sa.ForeignKey("proposals.id"), nullable=False),
        sa.Column("approver_id", sa.Text, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("approved_at_ts", sa.BigInteger, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.CheckConstraint("status IN ('approved', 'rejected')", name="ck_approvals_status"),
    )
    op.create_index("ix_approvals_proposal", "approvals", ["proposal_id"])

    # --- policy_grants ------------------------------------------------------
    op.create_table(
        "policy_grants",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("action_kind", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("requested_by", sa.Text, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("requested_at_ts", sa.BigInteger, nullable=False),
        sa.Column("granted_by", sa.Text, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("granted_at_ts", sa.BigInteger, nullable=True),
        sa.Column("valid_to_ts", sa.BigInteger, nullable=True),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'revoked', 'expired')",
            name="ck_policy_grants_status",
        ),
        # INV-01: self-approval — a grant cannot be approved by its own requester.
        sa.CheckConstraint(
            "status <> 'approved' "
            "OR (granted_by IS NOT NULL AND granted_at_ts IS NOT NULL "
            "AND valid_to_ts IS NOT NULL AND granted_by <> requested_by)",
            name="ck_policy_grants_no_self_approval",
        ),
    )
    op.create_index(
        "ix_policy_grants_lookup", "policy_grants", ["tenant_id", "action_kind", "scope"]
    )

    # --- comms_plans --------------------------------------------------------
    op.create_table(
        "comms_plans",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("audience", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("template_id", sa.Text, nullable=False),
        sa.Column("allowed_fields_json", sa.Text, nullable=False),
        sa.Column("cadence_minutes", sa.Integer, nullable=True),
        sa.Column("plan_hash", sa.Text, nullable=False),
        sa.Column("proposal_id", sa.Text, sa.ForeignKey("proposals.id"), nullable=True),
        sa.Column("approval_id", sa.Text, sa.ForeignKey("approvals.id"), nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'revoked', 'expired')",
            name="ck_comms_plans_status",
        ),
    )
    op.create_index("ix_comms_plans_proposal", "comms_plans", ["proposal_id"])

    # --- outbox -------------------------------------------------------------
    op.create_table(
        "outbox",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("action_kind", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("approval_id", sa.Text, sa.ForeignKey("approvals.id"), nullable=True),
        sa.Column(
            "policy_grant_id", sa.Text, sa.ForeignKey("policy_grants.id"), nullable=True
        ),
        sa.Column("comms_plan_id", sa.Text, sa.ForeignKey("comms_plans.id"), nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'sent', 'failed')", name="ck_outbox_status"
        ),
    )
    op.create_index("ix_outbox_approval", "outbox", ["approval_id"])

    # --- triggers -----------------------------------------------------------
    if is_sqlite:
        _upgrade_sqlite()
    else:
        _upgrade_postgres()


def _upgrade_sqlite() -> None:
    condition = _gate_condition("new", "CAST(strftime('%s','now') AS INTEGER)")
    op.execute(
        "CREATE TRIGGER trg_outbox_inv01 "
        "BEFORE INSERT ON outbox "
        "BEGIN "
        "SELECT RAISE(ABORT, 'INV-01: outbox row lacks valid authorization') "
        f"WHERE {condition}; "
        "END;"
    )
    op.execute(
        "CREATE TRIGGER trg_no_self_approval "
        "BEFORE INSERT ON approvals "
        "BEGIN "
        "SELECT RAISE(ABORT, 'INV-01: self-approval is not allowed') "
        "WHERE new.approver_id = (SELECT created_by FROM proposals WHERE id = new.proposal_id); "
        "END;"
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_mark_executed "
        "AFTER INSERT ON outbox "
        "WHEN new.approval_id IS NOT NULL "
        "BEGIN "
        "UPDATE proposals SET status = 'executed' "
        "WHERE id = (SELECT proposal_id FROM approvals WHERE id = new.approval_id); "
        "END;"
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_immutable "
        "BEFORE UPDATE OF tenant_id, case_id, action_kind, scope, payload_json, "
        "payload_hash, idempotency_key, approval_id, policy_grant_id, comms_plan_id "
        "ON outbox "
        "BEGIN "
        "SELECT RAISE(ABORT, 'INV-01: outbox authorization/payload is immutable'); "
        "END;"
    )


def _upgrade_postgres() -> None:
    condition = _gate_condition("NEW", "extract(epoch FROM now())::bigint")
    op.execute(
        "CREATE FUNCTION fn_outbox_inv01() RETURNS trigger AS $fn$ "
        "BEGIN "
        f"IF {condition} THEN "
        "RAISE EXCEPTION 'INV-01: outbox row lacks valid authorization'; "
        "END IF; "
        "RETURN NEW; "
        "END; "
        "$fn$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_inv01 "
        "BEFORE INSERT ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION fn_outbox_inv01()"
    )
    op.execute(
        "CREATE FUNCTION fn_no_self_approval() RETURNS trigger AS $fn$ "
        "BEGIN "
        "IF NEW.approver_id = (SELECT created_by FROM proposals WHERE id = NEW.proposal_id) THEN "
        "RAISE EXCEPTION 'INV-01: self-approval is not allowed'; "
        "END IF; "
        "RETURN NEW; "
        "END; "
        "$fn$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_no_self_approval "
        "BEFORE INSERT ON approvals "
        "FOR EACH ROW EXECUTE FUNCTION fn_no_self_approval()"
    )
    op.execute(
        "CREATE FUNCTION fn_outbox_mark_executed() RETURNS trigger AS $fn$ "
        "BEGIN "
        "IF NEW.approval_id IS NOT NULL THEN "
        "UPDATE proposals SET status = 'executed' "
        "WHERE id = (SELECT proposal_id FROM approvals WHERE id = NEW.approval_id); "
        "END IF; "
        "RETURN NEW; "
        "END; "
        "$fn$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_mark_executed "
        "AFTER INSERT ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION fn_outbox_mark_executed()"
    )
    op.execute(
        "CREATE FUNCTION fn_outbox_immutable() RETURNS trigger AS $fn$ "
        "BEGIN "
        "IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id "
        "OR NEW.case_id IS DISTINCT FROM OLD.case_id "
        "OR NEW.action_kind IS DISTINCT FROM OLD.action_kind "
        "OR NEW.scope IS DISTINCT FROM OLD.scope "
        "OR NEW.payload_json IS DISTINCT FROM OLD.payload_json "
        "OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash "
        "OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key "
        "OR NEW.approval_id IS DISTINCT FROM OLD.approval_id "
        "OR NEW.policy_grant_id IS DISTINCT FROM OLD.policy_grant_id "
        "OR NEW.comms_plan_id IS DISTINCT FROM OLD.comms_plan_id "
        "THEN "
        "RAISE EXCEPTION 'INV-01: outbox authorization/payload is immutable'; "
        "END IF; "
        "RETURN NEW; "
        "END; "
        "$fn$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_outbox_immutable "
        "BEFORE UPDATE ON outbox "
        "FOR EACH ROW EXECUTE FUNCTION fn_outbox_immutable()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if is_sqlite:
        for name in (
            "trg_outbox_immutable",
            "trg_outbox_mark_executed",
            "trg_no_self_approval",
            "trg_outbox_inv01",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    else:
        for trigger, table in (
            ("trg_outbox_immutable", "outbox"),
            ("trg_outbox_mark_executed", "outbox"),
            ("trg_outbox_inv01", "outbox"),
            ("trg_no_self_approval", "approvals"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for fn in (
            "fn_outbox_immutable",
            "fn_outbox_mark_executed",
            "fn_outbox_inv01",
            "fn_no_self_approval",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {fn}()")

    # Drop indexes, then tables in FK-safe order (children before parents).
    op.drop_index("ix_outbox_approval", table_name="outbox")
    op.drop_index("ix_comms_plans_proposal", table_name="comms_plans")
    op.drop_index("ix_policy_grants_lookup", table_name="policy_grants")
    op.drop_index("ix_approvals_proposal", table_name="approvals")
    op.drop_index("ix_proposals_case", table_name="proposals")

    op.drop_table("outbox")
    op.drop_table("comms_plans")
    op.drop_table("approvals")
    op.drop_table("policy_grants")
    op.drop_table("proposals")
