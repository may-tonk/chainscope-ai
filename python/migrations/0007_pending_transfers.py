"""Separate Alchemy page checkpoints and unverified data from formal statistics."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    job = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
    uuid = mysql.CHAR(36, charset="ascii", collation="ascii_bin")
    address = mysql.CHAR(42, charset="ascii", collation="ascii_bin")
    options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    op.create_table(
        "transfer_ingestion",
        sa.Column("job_id", job, primary_key=True),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("generation", uuid, nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("page_key", sa.Text),
        sa.Column("seen_keys", sa.JSON, nullable=False),
        sa.Column("pages", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("event_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("complete", sa.Boolean, nullable=False),
        sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id", "start_block"],
            ["coverage_windows.job_id", "coverage_windows.start_block"],
            name="fk_ingestion_window",
        ),
        **options,
    )
    op.create_table(
        "pending_transfers",
        sa.Column("job_id", job, primary_key=True),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
        sa.Column(
            "tx_hash",
            mysql.CHAR(66, charset="ascii", collation="ascii_bin"),
            primary_key=True,
        ),
        sa.Column("log_index", mysql.INTEGER(unsigned=True), primary_key=True),
        sa.Column("generation", uuid, nullable=False),
        sa.Column("block_number", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("from_address", address, nullable=False),
        sa.Column("to_address", address, nullable=False),
        sa.Column(
            "amount_raw",
            mysql.VARCHAR(78, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("block_time_utc", mysql.DATETIME(fsp=6)),
        sa.ForeignKeyConstraint(
            ["job_id", "start_block"],
            ["coverage_windows.job_id", "coverage_windows.start_block"],
            name="fk_pending_window",
        ),
        sa.CheckConstraint(
            "amount_raw REGEXP '^(0|[1-9][0-9]{0,77})$'",
            name="ck_pending_amount_digits",
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(amount_raw) < 78 OR amount_raw <= '115792089237316195423570985008687907853269984665640564039457584007913129639935'",
            name="ck_pending_amount_uint256",
        ),
        **options,
    )
    op.create_index(
        "ix_pending_generation",
        "pending_transfers",
        ["job_id", "start_block", "generation"],
    )
    op.create_table(
        "pool_discovery_progress",
        sa.Column("job_id", job, sa.ForeignKey("scan_jobs.job_id"), primary_key=True),
        sa.Column(
            "configuration",
            mysql.CHAR(64, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("scanned_through", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        **options,
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")


if __name__ == "__main__":
    raise SystemExit("迁移文件由 Alembic 加载，请执行 python -m chainscope init-db")
