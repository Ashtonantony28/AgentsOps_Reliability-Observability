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
    # an empty live counterexample is usually not computed yet (rarely computed empty; that case reads pending)
    op.execute("UPDATE policy_violations SET counterexample_status = 'pending' "
               "WHERE mode = 'live' AND CAST(counterexample AS TEXT) = '[]'")


def downgrade() -> None:
    with op.batch_alter_table("policy_violations") as batch:
        batch.drop_column("counterexample_status")
