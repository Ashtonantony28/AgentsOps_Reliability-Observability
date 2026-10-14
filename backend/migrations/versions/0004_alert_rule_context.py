"""alert_rules.context (policy_violations severity filter)

Revision ID: 0004_alert_rule_context
Revises: 0003_policy_not_null
Create Date: 2026-10-14

"""
import sqlalchemy as sa
from alembic import op

revision = "0004_alert_rule_context"
down_revision = "0003_policy_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert_rules", sa.Column("context", sa.JSON, nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("alert_rules", "context")
