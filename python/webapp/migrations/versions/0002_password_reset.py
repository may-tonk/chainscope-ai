"""账户库第二个迁移：邮箱验证表加入“重置密码”用途（2c）。

- purpose 允许 password_reset；
- 新增 user_id 列：重置请求对应的账户，账户删除时请求随之删除（外键 ON DELETE CASCADE）；
- 约束：重置请求必须有 user_id，注册请求不能有；
- 新增按 user_id 查找的索引。

不改动任何已有数据：现有的注册请求 user_id 为空，正好满足新约束。
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "auth_challenges",
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("app_users.user_id", ondelete="CASCADE"),
        ),
    )
    op.drop_constraint("ck_auth_challenges_purpose", "auth_challenges", type_="check")
    op.create_check_constraint(
        "ck_auth_challenges_purpose",
        "auth_challenges",
        "purpose IN ('register', 'password_reset')",
    )
    op.create_check_constraint(
        "ck_auth_challenges_user",
        "auth_challenges",
        "(purpose = 'password_reset') = (user_id IS NOT NULL)",
    )
    op.create_index("ix_auth_challenges_user", "auth_challenges", ["user_id"])


def downgrade():
    """回退到 0001：删除全部重置请求，再去掉新列和约束。只允许在测试库执行。"""
    if context.config.attributes.get("active_profile") != "test":
        raise RuntimeError("账户库回退会删除数据，只允许在测试库执行")
    op.execute("DELETE FROM auth_challenges WHERE purpose = 'password_reset'")
    op.drop_index("ix_auth_challenges_user", "auth_challenges")
    op.drop_constraint("ck_auth_challenges_user", "auth_challenges", type_="check")
    op.drop_constraint("ck_auth_challenges_purpose", "auth_challenges", type_="check")
    op.create_check_constraint(
        "ck_auth_challenges_purpose", "auth_challenges", "purpose IN ('register')"
    )
    op.drop_column("auth_challenges", "user_id")
