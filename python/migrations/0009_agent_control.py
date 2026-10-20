"""Add agent plans, runs, execution attempts and local authorization audit only."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    options = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_bin",
    }
    op.create_table(
        "agent_plans",
        sa.Column("plan_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("token_id", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("authorized_digest", sa.String(64)),
        sa.Column("run_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("owner_id", "operation_id", name="uq_agent_plan_operation"),
        **options,
    )
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("token_id", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=False),
        sa.Column("active_job", sa.String(32), unique=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("ticket", sa.String(36), nullable=False),
        sa.Column("pid", sa.BigInteger),
        sa.Column("process_started", sa.Double),
        sa.Column("stop_requested_at", sa.DateTime),
        sa.Column("heartbeat_at", sa.DateTime),
        sa.Column("accepted_at", sa.DateTime, nullable=False),
        **options,
    )
    op.create_index("ix_agent_run_owner", "agent_runs", ["owner_id", "accepted_at"])
    op.create_table(
        "agent_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("agent_runs.run_id"), nullable=False
        ),
        sa.Column("ticket", sa.String(36), nullable=False, unique=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("ended_at", sa.DateTime),
        sa.Column("payload", sa.JSON, nullable=False),
        **options,
    )
    op.create_table(
        "agent_events",
        sa.Column("event_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("plan_id", sa.String(36)),
        sa.Column("run_id", sa.String(36)),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        **options,
    )
    op.create_index("ix_agent_event_run", "agent_events", ["run_id", "event_id"])


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")
