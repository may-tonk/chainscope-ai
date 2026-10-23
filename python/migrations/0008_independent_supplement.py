"""Add independent Swap coverage and reusable block metadata; retain all old data."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    job = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
    options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    op.create_table(
        "supplement_jobs",
        sa.Column("job_id", job, sa.ForeignKey("scan_jobs.job_id"), primary_key=True),
        sa.Column("verified_through", mysql.BIGINT(unsigned=True)),
        sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        **options,
    )
    op.create_table(
        "swap_windows",
        sa.Column(
            "job_id", job, sa.ForeignKey("supplement_jobs.job_id"), primary_key=True
        ),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pool_set", mysql.CHAR(16, charset="ascii", collation="ascii_bin")),
        sa.Column("event_count", mysql.INTEGER(unsigned=True)),
        sa.Column("fingerprint", mysql.BINARY(32)),
        sa.Column("error_type", sa.String(64)),
        sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed')", name="ck_swap_window_status"
        ),
        sa.CheckConstraint("end_block >= start_block", name="ck_swap_window_range"),
        **options,
    )
    op.create_index(
        "ix_swap_window_status", "swap_windows", ["job_id", "status", "start_block"]
    )
    op.create_table(
        "block_headers",
        sa.Column("chain_id", mysql.INTEGER(unsigned=True), primary_key=True),
        sa.Column("block_number", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column(
            "block_hash",
            mysql.CHAR(66, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("block_time_utc", mysql.DATETIME(fsp=6), nullable=False),
        **options,
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")


if __name__ == "__main__":
    raise SystemExit("迁移文件由 Alembic 加载，请执行 python -m chainscope init-db")
