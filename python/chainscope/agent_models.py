"""Agent control records; no foreign key requires a token's scan job to exist yet."""

import sqlalchemy as sa

from .models import metadata

OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}
AGENT_TABLES = ("agent_plans", "agent_runs", "agent_attempts", "agent_events")

plans = sa.Table(
    "agent_plans",
    metadata,
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
    **OPTIONS,
)
runs = sa.Table(
    "agent_runs",
    metadata,
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
    sa.Index("ix_agent_run_owner", "owner_id", "accepted_at"),
    **OPTIONS,
)
attempts = sa.Table(
    "agent_attempts",
    metadata,
    sa.Column("attempt_id", sa.String(36), primary_key=True),
    sa.Column(
        "run_id", sa.String(36), sa.ForeignKey("agent_runs.run_id"), nullable=False
    ),
    sa.Column("ticket", sa.String(36), nullable=False, unique=True),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("started_at", sa.DateTime, nullable=False),
    sa.Column("ended_at", sa.DateTime),
    sa.Column("payload", sa.JSON, nullable=False),
    **OPTIONS,
)
events = sa.Table(
    "agent_events",
    metadata,
    sa.Column("event_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("owner_id", sa.String(36), nullable=False),
    sa.Column("plan_id", sa.String(36)),
    sa.Column("run_id", sa.String(36)),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("occurred_at", sa.DateTime, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Index("ix_agent_event_run", "run_id", "event_id"),
    **OPTIONS,
)
