"""Scan jobs with coverage windows: durable progress and resumable collection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ascii42 = mysql.CHAR(42, charset="ascii", collation="ascii_bin")
batch_id_type = mysql.CHAR(36, charset="ascii", collation="ascii_bin")
job_id_type = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade():
    op.add_column("collection_batches", sa.Column("job_id", job_id_type))
    op.create_table(
        "scan_jobs",
        sa.Column("job_id", job_id_type, primary_key=True),
        sa.Column("chain_id", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("token_address", ascii42, nullable=False),
        sa.Column("token_decimals", mysql.TINYINT(unsigned=True)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("window_size", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("parser_version", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("verified_through", mysql.BIGINT(unsigned=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint(
            "chain_id", "token_address", "source", name="uq_job_target"
        ),
        sa.CheckConstraint("status IN ('active', 'paused')", name="ck_job_status"),
        sa.CheckConstraint("window_size > 0", name="ck_job_window_size"),
        **options,
    )
    op.create_table(
        "coverage_windows",
        sa.Column(
            "job_id",
            job_id_type,
            sa.ForeignKey("scan_jobs.job_id"),
            primary_key=True,
        ),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_id", batch_id_type),
        sa.Column(
            "attempts",
            mysql.INTEGER(unsigned=True),
            nullable=False,
            server_default="0",
        ),
        sa.Column("event_count", mysql.INTEGER(unsigned=True)),
        sa.Column("inserted_count", mysql.INTEGER(unsigned=True)),
        sa.Column(
            "amount_sum", mysql.VARCHAR(90, charset="ascii", collation="ascii_bin")
        ),
        sa.Column("fingerprint", mysql.BINARY(32)),
        sa.Column("source_head", mysql.BIGINT(unsigned=True)),
        sa.Column("parser_version", mysql.SMALLINT(unsigned=True)),
        sa.Column("reconcile", sa.String(16)),
        sa.Column("error_type", sa.String(64)),
        sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_window_status",
        ),
        sa.CheckConstraint("end_block >= start_block", name="ck_window_range"),
        sa.Index("ix_window_status", "job_id", "status", "start_block"),
        **options,
    )
    op.create_table(
        "window_events",
        sa.Column("job_id", job_id_type, primary_key=True),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column(
            "event_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("transfer_events.event_id"),
            primary_key=True,
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "start_block"],
            ["coverage_windows.job_id", "coverage_windows.start_block"],
            name="fk_window_events_window",
        ),
        **options,
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")


if __name__ == "__main__":
    raise SystemExit("迁移文件由 Alembic 加载，请执行 python -m chainscope init-db")
