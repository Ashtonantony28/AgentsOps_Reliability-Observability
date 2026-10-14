"""policy_violations.counterexample_status: pending, minimal, fallback or none

Revision ID: 0006_counterexample_status
Revises: 0005_policy_archive
Create Date: 2026-10-14

"""
import sqlalchemy as sa
from alembic import op

revision = "0006_counterexample_status"
down_revision = "0005_policy_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("policy_violations", sa.Column("counterexample_status", sa.String(16), nullable=False,
                                                 server_default="minimal"))
    # an empty live counterexample is pending while its stream is open; on a closed one the batch check left it
    # empty, so none (rarely a computed empty one)
    open_ = "REPLACE(COALESCE(CAST(meta AS TEXT), ''), ' ', '') LIKE '%\"stream\":\"open\"%'"
    op.execute("UPDATE policy_violations SET counterexample_status = CASE WHEN trace_id IN "
               f"(SELECT id FROM traces WHERE {open_}) THEN 'pending' ELSE 'none' END "
               "WHERE mode = 'live' AND CAST(counterexample AS TEXT) = '[]'")


def downgrade() -> None:
    with op.batch_alter_table("policy_violations") as batch:
        batch.drop_column("counterexample_status")
