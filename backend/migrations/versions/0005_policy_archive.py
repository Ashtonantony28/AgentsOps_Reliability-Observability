"""trace_policies.archived_at: deleting a policy archives it and keeps its violations

Revision ID: 0005_policy_archive
Revises: 0004_alert_rule_context
Create Date: 2026-10-14

"""
import sqlalchemy as sa
from alembic import op

revision = "0005_policy_archive"
down_revision = "0004_alert_rule_context"
branch_labels = None
depends_on = None

LIVE = sa.text("archived_at IS NULL")


def upgrade() -> None:
    op.add_column("trace_policies", sa.Column("archived_at", sa.DateTime, nullable=True))
    op.drop_index("ix_trace_policy_org_name", table_name="trace_policies")
    op.create_index("ix_trace_policy_org_name", "trace_policies", ["org_id", "name"], unique=True,
                    postgresql_where=LIVE, sqlite_where=LIVE)


def downgrade() -> None:
    # archived names may repeat, which the full unique index cannot hold: keep them disabled under their ids
    op.execute("UPDATE trace_policies SET name = id, enabled = false WHERE archived_at IS NOT NULL")
    op.drop_index("ix_trace_policy_org_name", table_name="trace_policies")
    op.create_index("ix_trace_policy_org_name", "trace_policies", ["org_id", "name"], unique=True)
    with op.batch_alter_table("trace_policies") as batch:
        batch.drop_column("archived_at")
