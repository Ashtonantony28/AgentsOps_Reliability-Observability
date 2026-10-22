"""SG-P0 platform contracts: invites, oauth_states, integrations.signing_secret, evaluation_sets.runner

Revision ID: 0007_platform_contracts
Revises: 0006_counterexample_status
Create Date: 2026-10-21

"""
import sqlalchemy as sa
from alembic import op

revision = "0007_platform_contracts"
down_revision = "0006_counterexample_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_invite_org_email", "invites", ["org_id", "email"])
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.add_column("integrations", sa.Column("signing_secret", sa.String(64), nullable=True))
    op.add_column("evaluation_sets", sa.Column("runner", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("evaluation_sets") as batch:
        batch.drop_column("runner")
    with op.batch_alter_table("integrations") as batch:
        batch.drop_column("signing_secret")
    op.drop_table("oauth_states")
    op.drop_index("ix_invite_org_email", table_name="invites")
    op.drop_table("invites")
