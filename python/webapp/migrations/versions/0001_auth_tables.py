"""账户库第一个迁移：创建 5 张表。

- app_users：账户
- auth_challenges：注册时的邮箱验证
- auth_sessions：登录会话
- auth_rate_limits：共享限速计数
- auth_events：认证审计

每一列的含义见 python/webapp/db/tables.py 的注释。这里的建表语句写死、不引用 tables.py：
迁移一旦执行过就不再修改，以后 tables.py 怎么改，这个文件都保持原样，
结构变化通过新增 0002、0003…… 来完成。
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql as pg

# Alembic 用这几个变量把迁移串成一条链：本迁移编号 0001，前面没有迁移。
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """升级：按顺序建表。app_users 要先建，因为另外两张表有外键指向它。"""
    uuid = pg.UUID(as_uuid=True)
    utc = sa.DateTime(timezone=True)
    new_uuid = sa.text("gen_random_uuid()")
    now = sa.text("now()")
    op.create_table(
        "app_users",
        sa.Column("user_id", uuid, primary_key=True, server_default=new_uuid),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("email_key", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("email_verified_at", utc, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("auth_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("password_changed_at", utc, nullable=False),
        sa.Column("created_at", utc, nullable=False, server_default=now),
        sa.Column("updated_at", utc, nullable=False, server_default=now),
        sa.CheckConstraint("email_key = lower(email)", name="ck_app_users_email_key"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_app_users_status"
        ),
        sa.CheckConstraint("auth_version >= 1", name="ck_app_users_auth_version"),
    )
    op.create_table(
        "auth_challenges",
        sa.Column("challenge_id", uuid, primary_key=True, server_default=new_uuid),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("email_key", sa.String(254), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("code_hmac", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.SmallInteger, nullable=False),
        sa.Column("flow_binding", sa.LargeBinary, nullable=False),
        sa.Column(
            "failed_attempts", sa.SmallInteger, nullable=False, server_default="0"
        ),
        sa.Column("created_at", utc, nullable=False, server_default=now),
        sa.Column("expires_at", utc, nullable=False),
        sa.Column("verified_at", utc),
        sa.Column("consumed_at", utc),
        sa.Column("invalidated_at", utc),
        sa.Column("registration_token_hash", sa.LargeBinary, unique=True),
        sa.Column("registration_expires_at", utc),
        sa.CheckConstraint(
            "purpose IN ('register')", name="ck_auth_challenges_purpose"
        ),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_auth_challenges_failures"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR verified_at IS NOT NULL",
            name="ck_auth_challenges_consumed_after_verified",
        ),
        sa.CheckConstraint(
            "(registration_token_hash IS NULL) = (registration_expires_at IS NULL)",
            name="ck_auth_challenges_token_expiry",
        ),
    )
    op.create_index(
        "ix_auth_challenges_email", "auth_challenges", ["email_key", "created_at"]
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", uuid, primary_key=True, server_default=new_uuid),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("key_version", sa.SmallInteger, nullable=False),
        sa.Column(
            "user_id", uuid, sa.ForeignKey("app_users.user_id", ondelete="CASCADE")
        ),
        sa.Column("auth_version", sa.Integer),
        sa.Column("csrf_hash", sa.LargeBinary, nullable=False),
        sa.Column("created_at", utc, nullable=False, server_default=now),
        sa.Column("last_seen_at", utc, nullable=False, server_default=now),
        sa.Column("absolute_expires_at", utc, nullable=False),
        sa.Column("revoked_at", utc),
        sa.CheckConstraint(
            "kind IN ('anonymous', 'user')", name="ck_auth_sessions_kind"
        ),
        sa.CheckConstraint(
            "(kind = 'user') = (user_id IS NOT NULL AND auth_version IS NOT NULL)",
            name="ck_auth_sessions_user",
        ),
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"])
    op.create_table(
        "auth_rate_limits",
        sa.Column("purpose", sa.String(32), primary_key=True),
        sa.Column("subject_key", sa.LargeBinary, primary_key=True),
        sa.Column("window_start", utc, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("blocked_until", utc),
        sa.Column("updated_at", utc, nullable=False, server_default=now),
        sa.CheckConstraint("attempts >= 0", name="ck_auth_rate_limits_attempts"),
    )
    op.create_index("ix_auth_rate_limits_updated", "auth_rate_limits", ["updated_at"])
    op.create_table(
        "auth_events",
        sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("occurred_at", utc, nullable=False, server_default=now),
        sa.Column(
            "user_id", uuid, sa.ForeignKey("app_users.user_id", ondelete="SET NULL")
        ),
        sa.Column("request_id", sa.String(64)),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("source", sa.String(64)),
        sa.Column("reason", sa.String(48)),
    )
    op.create_index("ix_auth_events_time", "auth_events", ["occurred_at"])
    op.create_index("ix_auth_events_user", "auth_events", ["user_id"])


def downgrade():
    """回退：删掉这 5 张表。

    删表等于删光全部账户，所以只允许在测试库执行（env.py 记下的 active_profile 必须是 test）；
    开发库、以后的正式库都会被直接拒绝。
    删除顺序和建表相反：先删有外键指向 app_users 的表，最后删 app_users。
    """
    if context.config.attributes.get("active_profile") != "test":
        raise RuntimeError("账户库回退会删除全部账户，只允许在测试库执行")
    for table in (
        "auth_events",
        "auth_rate_limits",
        "auth_sessions",
        "auth_challenges",
        "app_users",
    ):
        op.drop_table(table)
