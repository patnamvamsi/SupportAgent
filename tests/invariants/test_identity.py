"""Identity invariants (03 §2.1, WP-012): one bootstrap admin per tenant.

The partial unique index ``ux_role_assignments_bootstrap_admin`` (migration
0005) makes "the first user becomes admin once" hold even when two first
logins race — on both engines.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text


def test_first_admin_unique_index_blocks_a_second_bootstrap_admin(db: Any) -> None:
    # Two concurrent first logins: the partial unique index lets one win.
    with db.begin() as conn:
        for uid in ("u1", "u2"):
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                    "VALUES (:id, 'acme', :id, :id, 1, 0)"
                ),
                {"id": uid},
            )
        conn.execute(
            text(
                "INSERT INTO role_assignments (user_id, tenant_id, role, scope, granted_ts) "
                "VALUES ('u1', 'acme', 'admin', '*', 0)"
            )
        )
    with pytest.raises(Exception, match=r"(?i)unique|duplicate"), db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO role_assignments (user_id, tenant_id, role, scope, granted_ts) "
                "VALUES ('u2', 'acme', 'admin', '*', 0)"
            )
        )

    # An admin granted by another admin (granted_by set) is not a bootstrap row.
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO role_assignments "
                "(user_id, tenant_id, role, scope, granted_by, granted_ts) "
                "VALUES ('u2', 'acme', 'admin', '*', 'u1', 0)"
            )
        )
