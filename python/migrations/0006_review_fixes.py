"""Deploy block and end block of a job; fingerprint version and pool set of a window."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scan_jobs", sa.Column("deploy_block", mysql.BIGINT(unsigned=True)))
    op.add_column("scan_jobs", sa.Column("end_block", mysql.BIGINT(unsigned=True)))
    op.add_column(
        "coverage_windows",
        sa.Column("fingerprint_version", mysql.SMALLINT(unsigned=True)),
    )
    op.add_column(
        "coverage_windows",
        sa.Column("pool_set", mysql.CHAR(16, charset="ascii", collation="ascii_bin")),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")


if __name__ == "__main__":
    raise SystemExit("迁移文件由 Alembic 加载，请执行 python -m chainscope init-db")
