"""账户库（PostgreSQL）的表结构定义。

这里描述的是“程序期望的表长什么样”；真正建表的语句固定写在迁移文件
python/webapp/migrations/versions/0001_auth_tables.py 里。改这个文件不会自动改数据库，
结构变化要新增迁移。测试会核对两边是否一致。

和 MySQL 采集库的表定义（chainscope/models.py）完全分开，用各自的 MetaData。
约定：
- 时间一律用带时区的 timestamptz，按 UTC 保存，页面显示时再换成北京时间；
- 密码、验证码、令牌等秘密从不保存原值，只保存哈希或 HMAC（LargeBinary 即 PostgreSQL 的 bytea）。
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

metadata = sa.MetaData()
uuid = pg.UUID(as_uuid=True)  # 在 Python 里取出来直接是 uuid.UUID 对象
utc = sa.DateTime(timezone=True)  # 对应 PostgreSQL 的 timestamptz
new_uuid = sa.text("gen_random_uuid()")  # 由数据库生成随机 UUID（PostgreSQL 13 起内置）
now = sa.text("now()")  # 由数据库填当前时间

# ── 账户表：一行就是一个长期存在的账户 ──
users = sa.Table(
    "app_users",
    metadata,
    # 随机 UUID 作为用户编号，不用自增数字，别人猜不出也数不出有多少用户。
    sa.Column("user_id", uuid, primary_key=True, server_default=new_uuid),
    # email：显示用的地址；email_key：转成小写后的登录标识，唯一约束保证一个邮箱只有一个账户。
    # 254 是邮箱地址的标准最大长度。
    sa.Column("email", sa.String(254), nullable=False),
    sa.Column("email_key", sa.String(254), nullable=False, unique=True),
    # Argon2id 哈希全文（含算法参数和盐），不是明文密码。
    sa.Column("password_hash", sa.Text, nullable=False),
    # 账户只在验证邮箱之后才创建，所以这一列不能为空。
    sa.Column("email_verified_at", utc, nullable=False),
    sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    # 认证版本号：修改密码、禁用账户、退出所有设备时加 1。
    # 每个会话记录了创建时的版本号，版本对不上的旧会话立即失效。
    sa.Column("auth_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("password_changed_at", utc, nullable=False),
    sa.Column("created_at", utc, nullable=False, server_default=now),
    sa.Column("updated_at", utc, nullable=False, server_default=now),
    # 数据库层面的约束：即使程序有 bug，也写不进违反规则的数据。
    sa.CheckConstraint("email_key = lower(email)", name="ck_app_users_email_key"),
    sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_app_users_status"),
    sa.CheckConstraint("auth_version >= 1", name="ck_app_users_auth_version"),
)

# ── 邮箱验证表：先发邮箱验证码，验证通过后发一次性的凭证，凭它完成下一步 ──
# 两种用途共用这张表：注册（register，凭证用来设置密码、创建账户）
# 和重置密码（password_reset，凭证用来设置新密码；迁移 0002 加入）。
challenges = sa.Table(
    "auth_challenges",
    metadata,
    sa.Column("challenge_id", uuid, primary_key=True, server_default=new_uuid),
    sa.Column("email", sa.String(254), nullable=False),
    sa.Column("email_key", sa.String(254), nullable=False),
    # 用途：register（注册）或 password_reset（重置密码）。
    sa.Column("purpose", sa.String(16), nullable=False),
    # 重置密码请求对应的账户（迁移 0002）。记用户编号而不只是邮箱：账户被注销后，
    # 它的重置请求随外键一起删除，不会被拿去改同一邮箱新注册的账户。注册请求没有这一列。
    sa.Column("user_id", uuid, sa.ForeignKey("app_users.user_id", ondelete="CASCADE")),
    # 验证码只存 HMAC（用服务端密钥计算的校验值），数据库泄漏也无法直接看出验证码。
    sa.Column("code_hmac", sa.LargeBinary, nullable=False),
    # 计算 HMAC 用的是第几版密钥，以后换密钥时能区分新旧记录。
    sa.Column("key_version", sa.SmallInteger, nullable=False),
    # 绑定发起注册的浏览器流程：别的浏览器拿到验证码也用不了。
    sa.Column("flow_binding", sa.LargeBinary, nullable=False),
    # 输错次数，达到上限后这个请求作废。
    sa.Column("failed_attempts", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("created_at", utc, nullable=False, server_default=now),
    sa.Column("expires_at", utc, nullable=False),  # 验证码过期时间（初始设计 5 分钟）
    sa.Column("verified_at", utc),  # 验证码验证通过的时间
    sa.Column("consumed_at", utc),  # 凭证被用掉（账户已创建或密码已重置）的时间
    # 重发验证码或注销账户时，旧请求作废的时间。
    sa.Column("invalidated_at", utc),
    # 注册凭证或重置凭证只存哈希；唯一约束防止重复。列名沿用第一版的 registration_*。
    sa.Column("registration_token_hash", sa.LargeBinary, unique=True),
    sa.Column("registration_expires_at", utc),
    sa.CheckConstraint(
        "purpose IN ('register', 'password_reset')", name="ck_auth_challenges_purpose"
    ),
    # 重置请求必须有用户编号，注册请求不能有。
    sa.CheckConstraint(
        "(purpose = 'password_reset') = (user_id IS NOT NULL)",
        name="ck_auth_challenges_user",
    ),
    sa.CheckConstraint("failed_attempts >= 0", name="ck_auth_challenges_failures"),
    # 没验证过的请求不可能被“用掉”。
    sa.CheckConstraint(
        "consumed_at IS NULL OR verified_at IS NOT NULL",
        name="ck_auth_challenges_consumed_after_verified",
    ),
    # 凭证和它的过期时间必须同时有、或同时没有。
    sa.CheckConstraint(
        "(registration_token_hash IS NULL) = (registration_expires_at IS NULL)",
        name="ck_auth_challenges_token_expiry",
    ),
    # 按邮箱查最近的请求（限制重发频率、作废旧请求）时用的索引。
    sa.Index("ix_auth_challenges_email", "email_key", "created_at"),
    # 按账户作废重置请求（登录状态下改了密码时）用的索引。
    sa.Index("ix_auth_challenges_user", "user_id"),
)

# ── 会话表：服务端保存登录状态，可以随时撤销 ──
# 两种会话：anonymous（登录前的匿名流程，用来做注册、登录页的 CSRF 保护）和 user（已登录）。
sessions = sa.Table(
    "auth_sessions",
    metadata,
    sa.Column("session_id", uuid, primary_key=True, server_default=new_uuid),
    sa.Column("kind", sa.String(16), nullable=False),
    # 浏览器 Cookie 里存原始令牌，数据库只存它的 HMAC 指纹；数据库泄漏也无法冒充登录。
    sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
    sa.Column("key_version", sa.SmallInteger, nullable=False),
    # 账户被删除时（ondelete=CASCADE），它的会话由数据库自动一起删除。
    sa.Column("user_id", uuid, sa.ForeignKey("app_users.user_id", ondelete="CASCADE")),
    # 创建会话时账户的认证版本号，和 app_users.auth_version 比对。
    sa.Column("auth_version", sa.Integer),
    sa.Column("csrf_hash", sa.LargeBinary, nullable=False),
    sa.Column("created_at", utc, nullable=False, server_default=now),
    # 最近活动时间，用来判断空闲超时（初始设计 24 小时）。
    sa.Column("last_seen_at", utc, nullable=False, server_default=now),
    # 绝对过期时间（初始设计 7 天），不管活不活跃，到点就失效。
    sa.Column("absolute_expires_at", utc, nullable=False),
    sa.Column("revoked_at", utc),  # 退出登录等主动撤销的时间
    sa.CheckConstraint("kind IN ('anonymous', 'user')", name="ck_auth_sessions_kind"),
    # 已登录会话必须有用户和版本号；匿名会话两者都不能有。
    sa.CheckConstraint(
        "(kind = 'user') = (user_id IS NOT NULL AND auth_version IS NOT NULL)",
        name="ck_auth_sessions_user",
    ),
    # 按用户查会话（退出所有设备、注销账户）时用的索引。
    sa.Index("ix_auth_sessions_user", "user_id"),
)

# ── 限速表：所有后端进程共用的失败计数，防止暴力猜密码和恶意锁号 ──
# 存在数据库里而不是 Python 内存里，开多个后端进程时计数也是同一份。
rate_limits = sa.Table(
    "auth_rate_limits",
    metadata,
    # purpose：计数的种类，例如“某邮箱＋某来源的登录失败”。
    sa.Column("purpose", sa.String(32), primary_key=True),
    # subject_key：被计数对象（邮箱、IP 等）的 HMAC，不直接存邮箱或 IP。
    sa.Column("subject_key", sa.LargeBinary, primary_key=True),
    sa.Column("window_start", utc, nullable=False),  # 当前计数窗口的开始时间
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("blocked_until", utc),  # 冷却到什么时候
    sa.Column("updated_at", utc, nullable=False, server_default=now),
    sa.CheckConstraint("attempts >= 0", name="ck_auth_rate_limits_attempts"),
    # 定期清理过期计数时用的索引。
    sa.Index("ix_auth_rate_limits_updated", "updated_at"),
)

# ── 审计表：记录登录、注册、退出等事件，不保存任何秘密 ──
events = sa.Table(
    "auth_events",
    metadata,
    # 自增编号（Identity），由数据库序列生成。
    sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("occurred_at", utc, nullable=False, server_default=now),
    # 账户被删除时（ondelete=SET NULL），只把这一列清空，事件本身按保留期再清理。
    sa.Column("user_id", uuid, sa.ForeignKey("app_users.user_id", ondelete="SET NULL")),
    sa.Column("request_id", sa.String(64)),  # 请求编号，用来和日志对应
    sa.Column("event_type", sa.String(48), nullable=False),  # 如 login_failed
    sa.Column("source", sa.String(64)),  # 脱敏后的来源信息
    sa.Column("reason", sa.String(48)),  # 内部原因码，不对外显示
    sa.Index("ix_auth_events_time", "occurred_at"),
    sa.Index("ix_auth_events_user", "user_id"),
)

# 所有表名，测试用来确认“库里只有我们的表”。
TABLES = tuple(metadata.tables)
