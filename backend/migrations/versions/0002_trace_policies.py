"""trace policies, violations, span seq/attrs

Revision ID: 0002_trace_policies
Revises: 0001_init
Create Date: 2026-10-13

"""
from alembic import op
import sqlalchemy as sa


revision = "0002_trace_policies"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spans", sa.Column("seq", sa.Integer, nullable=True))
    op.add_column("spans", sa.Column("attrs", sa.JSON, nullable=True))
    op.execute(
        "UPDATE spans SET seq = ordered.rn - 1 FROM ("
        " SELECT id, ROW_NUMBER() OVER (PARTITION BY trace_id ORDER BY started_at, id) AS rn FROM spans"
        ") AS ordered WHERE spans.id = ordered.id"
    )
    op.create_index("ix_span_trace_seq", "spans", ["trace_id", "seq"], unique=True)

    op.create_table(
        "trace_policies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("severity", sa.String(16), default="medium"),
        sa.Column("action", sa.String(16), default="log"),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("environment", sa.String(32), nullable=True),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("version", sa.Integer, default=1),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_trace_policies_org_id", "trace_policies", ["org_id"])
    op.create_index("ix_trace_policy_org_name", "trace_policies", ["org_id", "name"], unique=True)

    op.create_table(
        "policy_violations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("trace_id", sa.String(), sa.ForeignKey("traces.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("policy_id", sa.String(), sa.ForeignKey("trace_policies.id", ondelete="CASCADE")),
        sa.Column("policy_version", sa.Integer, nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("at_seq", sa.Integer, nullable=False),
        sa.Column("counterexample", sa.JSON, default=list),
        sa.Column("message", sa.String, default=""),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("detected_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_policy_violations_trace_id", "policy_violations", ["trace_id"])
    op.create_index("ix_policy_violations_policy_id", "policy_violations", ["policy_id"])
    op.create_index("ix_violation_org_detected", "policy_violations", ["org_id", "detected_at"])
    op.create_index(
        "ix_violation_trace_policy_version", "policy_violations",
        ["trace_id", "policy_id", "policy_version"], unique=True,
    )


def downgrade() -> None:
    op.drop_table("policy_violations")
    op.drop_table("trace_policies")
    op.drop_index("ix_span_trace_seq", table_name="spans")
    op.drop_column("spans", "attrs")
    op.drop_column("spans", "seq")
