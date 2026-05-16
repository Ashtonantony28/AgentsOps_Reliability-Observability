"""init schema

Revision ID: 0001_init
Revises:
Create Date: 2026-01-01

"""
from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("tier", sa.String(32), default="free"),
        sa.Column("retention_days", sa.Integer, default=90),
        sa.Column("rate_limit_per_min", sa.Integer, default=600),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), default=""),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "org_memberships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("role", sa.String(32), default="developer"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_membership_org_user", "org_memberships", ["org_id", "user_id"], unique=True)

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255)),
        sa.Column("key_prefix", sa.String(16)),
        sa.Column("key_hash", sa.String(128)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("refresh_token_hash", sa.String(255)),
        sa.Column("user_agent", sa.String(512), default=""),
        sa.Column("ip_address", sa.String(64), default=""),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_refresh_token_hash", "sessions", ["refresh_token_hash"])

    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String, default=""),
        sa.Column("model_provider", sa.String(64), default="anthropic"),
        sa.Column("model_name", sa.String(128), default="claude-opus-4-7"),
        sa.Column("environment", sa.String(32), default="dev"),
        sa.Column("current_version", sa.String(64), default="1.0.0"),
        sa.Column("tags", sa.JSON, default=list),
        sa.Column("allowed_tools", sa.JSON, default=list),
        sa.Column("eval_threshold", sa.Float, default=0.8),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_agents_org_id", "agents", ["org_id"])

    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("version", sa.String(64)),
        sa.Column("model_name", sa.String(128)),
        sa.Column("prompt_template", sa.String, default=""),
        sa.Column("config", sa.JSON, default=dict),
        sa.Column("environment", sa.String(32), default="dev"),
        sa.Column("promoted_from", sa.String(32), nullable=True),
        sa.Column("promoted_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])

    op.create_table(
        "agent_dependencies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("parent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("child_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("call_count", sa.Integer, default=0),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_dep_pair", "agent_dependencies", ["parent_id", "child_id"], unique=True)

    op.create_table(
        "traces",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("trace_uid", sa.String(128)),
        sa.Column("status", sa.String(32), default="success"),
        sa.Column("input", sa.String, default=""),
        sa.Column("output", sa.String, default=""),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("latency_ms", sa.Integer, default=0),
        sa.Column("tokens_input", sa.Integer, default=0),
        sa.Column("tokens_output", sa.Integer, default=0),
        sa.Column("cost_usd", sa.Float, default=0.0),
        sa.Column("environment", sa.String(32), default="dev"),
        sa.Column("agent_version", sa.String(64), default="1.0.0"),
        sa.Column("pii_flags", sa.JSON, default=list),
        sa.Column("injection_score", sa.Float, default=0.0),
        sa.Column("meta", sa.JSON, default=dict),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_traces_org_id", "traces", ["org_id"])
    op.create_index("ix_traces_agent_id", "traces", ["agent_id"])
    op.create_index("ix_traces_status", "traces", ["status"])
    op.create_index("ix_traces_session_id", "traces", ["session_id"])
    op.create_index("ix_traces_trace_uid", "traces", ["trace_uid"])
    op.create_index("ix_traces_created_at", "traces", ["created_at"])
    op.create_index("ix_traces_environment", "traces", ["environment"])
    op.create_index("ix_trace_agent_created", "traces", ["agent_id", "created_at"])
    op.create_index("ix_trace_org_created", "traces", ["org_id", "created_at"])

    op.create_table(
        "spans",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("trace_id", sa.String(), sa.ForeignKey("traces.id", ondelete="CASCADE")),
        sa.Column("parent_span_id", sa.String, nullable=True),
        sa.Column("kind", sa.String(32), default="tool"),
        sa.Column("name", sa.String(255)),
        sa.Column("input", sa.JSON, default=dict),
        sa.Column("output", sa.JSON, default=dict),
        sa.Column("latency_ms", sa.Integer, default=0),
        sa.Column("status", sa.String(32), default="success"),
        sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_spans_trace_id", "spans", ["trace_id"])

    op.create_table(
        "evaluation_sets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255)),
        sa.Column("description", sa.String, default=""),
        sa.Column("evaluators", sa.JSON, default=list),
        sa.Column("schedule_cron", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_evaluation_sets_org_id", "evaluation_sets", ["org_id"])

    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("set_id", sa.String(), sa.ForeignKey("evaluation_sets.id", ondelete="CASCADE")),
        sa.Column("input", sa.String),
        sa.Column("expected_output", sa.String, default=""),
        sa.Column("tags", sa.JSON, default=list),
        sa.Column("weight", sa.Float, default=1.0),
    )
    op.create_index("ix_evaluation_cases_set_id", "evaluation_cases", ["set_id"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("set_id", sa.String(), sa.ForeignKey("evaluation_sets.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("agent_version", sa.String(64), default="1.0.0"),
        sa.Column("status", sa.String(32), default="pending"),
        sa.Column("score", sa.Float, default=0.0),
        sa.Column("passed", sa.Integer, default=0),
        sa.Column("failed", sa.Integer, default=0),
        sa.Column("total", sa.Integer, default=0),
        sa.Column("summary", sa.JSON, default=dict),
        sa.Column("triggered_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_evaluation_runs_set_id", "evaluation_runs", ["set_id"])
    op.create_index("ix_evaluation_runs_agent_id", "evaluation_runs", ["agent_id"])

    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE")),
        sa.Column("case_id", sa.String(), sa.ForeignKey("evaluation_cases.id", ondelete="CASCADE")),
        sa.Column("actual_output", sa.String, default=""),
        sa.Column("passed", sa.Boolean, default=False),
        sa.Column("score", sa.Float, default=0.0),
        sa.Column("scores_by_evaluator", sa.JSON, default=dict),
        sa.Column("reason", sa.String, default=""),
        sa.Column("latency_ms", sa.Integer, default=0),
    )
    op.create_index("ix_evaluation_results_run_id", "evaluation_results", ["run_id"])

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255)),
        sa.Column("metric", sa.String(64)),
        sa.Column("operator", sa.String(8)),
        sa.Column("threshold", sa.Float),
        sa.Column("window_minutes", sa.Integer, default=15),
        sa.Column("severity", sa.String(16), default="warning"),
        sa.Column("channels", sa.JSON, default=list),
        sa.Column("runbook_url", sa.String, default=""),
        sa.Column("dedup_minutes", sa.Integer, default=30),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_alert_rules_org_id", "alert_rules", ["org_id"])
    op.create_index("ix_alert_rules_agent_id", "alert_rules", ["agent_id"])

    op.create_table(
        "alert_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("rule_id", sa.String(), sa.ForeignKey("alert_rules.id", ondelete="CASCADE")),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(32), default="firing"),
        sa.Column("value", sa.Float, default=0.0),
        sa.Column("message", sa.String, default=""),
        sa.Column("assignee_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("fired_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("context", sa.JSON, default=dict),
    )
    op.create_index("ix_alert_events_rule_id", "alert_events", ["rule_id"])
    op.create_index("ix_alert_events_org_id", "alert_events", ["org_id"])
    op.create_index("ix_alert_events_status", "alert_events", ["status"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(255)),
        sa.Column("status", sa.String(32), default="open"),
        sa.Column("severity", sa.String(16), default="warning"),
        sa.Column("timeline", sa.JSON, default=list),
        sa.Column("narrative", sa.String, default=""),
        sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_incidents_org_id", "incidents", ["org_id"])
    op.create_index("ix_incidents_status", "incidents", ["status"])

    op.create_table(
        "integrations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(64)),
        sa.Column("name", sa.String(255)),
        sa.Column("config", sa.JSON, default=dict),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("last_sync_at", sa.DateTime, nullable=True),
        sa.Column("last_status", sa.String(32), default="unknown"),
        sa.Column("error_count", sa.Integer, default=0),
        sa.Column("last_error", sa.String, default=""),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_integrations_org_id", "integrations", ["org_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("actor_type", sa.String(32), default="user"),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(128)),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("rationale", sa.String, default=""),
        sa.Column("details", sa.JSON, default=dict),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "policy_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255)),
        sa.Column("description", sa.String, default=""),
        sa.Column("rule", sa.JSON, default=dict),
        sa.Column("action", sa.String(32), default="block"),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_policy_rules_org_id", "policy_rules", ["org_id"])


def downgrade() -> None:
    for t in [
        "policy_rules", "audit_logs", "integrations", "incidents",
        "alert_events", "alert_rules", "evaluation_results", "evaluation_runs",
        "evaluation_cases", "evaluation_sets", "spans", "traces",
        "agent_dependencies", "agent_versions", "agents", "sessions",
        "api_keys", "org_memberships", "users", "organizations",
    ]:
        op.drop_table(t)
