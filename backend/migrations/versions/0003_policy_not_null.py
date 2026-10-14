"""policy tables: backfill, server defaults, NOT NULL

Revision ID: 0003_policy_not_null
Revises: 0002_trace_policies
Create Date: 2026-10-14

"""
import sqlalchemy as sa
from alembic import op

revision = "0003_policy_not_null"
down_revision = "0002_trace_policies"
branch_labels = None
depends_on = None

# (table, column, type, server default, default added here)
COLUMNS = [
    ("trace_policies", "severity", sa.String(16), "'medium'", True),
    ("trace_policies", "action", sa.String(16), "'log'", True),
    ("trace_policies", "created_at", sa.DateTime, "now()", False),
    ("trace_policies", "updated_at", sa.DateTime, "now()", False),
    ("policy_violations", "counterexample", sa.JSON, "'[]'", True),
    ("policy_violations", "message", sa.String, "''", True),
    ("policy_violations", "detected_at", sa.DateTime, "now()", False),
]


def upgrade() -> None:
    for table, col, type_, default, _ in COLUMNS:
        op.execute(f"UPDATE {table} SET {col} = {default} WHERE {col} IS NULL")
        op.alter_column(table, col, existing_type=type_, server_default=sa.text(default), nullable=False)


def downgrade() -> None:
    for table, col, type_, default, added in COLUMNS:
        op.alter_column(table, col, existing_type=type_, nullable=True,
                        **({"server_default": None} if added else {"existing_server_default": sa.text(default)}))
