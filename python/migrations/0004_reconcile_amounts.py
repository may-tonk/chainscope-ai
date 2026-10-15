"""Cross-source reconciliation evidence and numeric amount ordering."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ascii66 = mysql.CHAR(66, charset="ascii", collation="ascii_bin")
job_id_type = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade():
    op.add_column(
        "coverage_windows",
        sa.Column("receipt_checked", mysql.INTEGER(unsigned=True)),
    )
    op.add_column(
        "transfer_events",
        sa.Column(
            "amount_len",
            mysql.TINYINT(unsigned=True),
            sa.Computed("CHAR_LENGTH(amount_raw)", persisted=True),
        ),
    )
    op.create_index(
        "ix_transfer_amount",
        "transfer_events",
        ["chain_id", "token_address", "amount_len", "amount_raw"],
    )
    op.create_table(
        "window_diffs",
        sa.Column(
            "diff_id",
            mysql.BIGINT(unsigned=True),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("job_id", job_id_type, nullable=False),
        sa.Column("start_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("found_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("tx_hash", ascii66),
        sa.Column("log_index", mysql.INTEGER(unsigned=True)),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Index("ix_diff_window", "job_id", "start_block"),
        **options,
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")


if __name__ == "__main__":
    raise SystemExit("迁移文件由 Alembic 加载，请执行 python -m chainscope init-db")
