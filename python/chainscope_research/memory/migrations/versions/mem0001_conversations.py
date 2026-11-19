"""Memory 首次迁移：会话、轮次、消息与摘要，约束固定在本版本内。

研究业务 schema 已由环境准备创建；账户 public.app_users 必须先存在。
账户、checkpoint 结构和迁移版本不由此文件管理。已应用后通过后续版本改结构。
"""

from alembic import context, op

revision = "mem0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = (
    # 用户外键保证账号不存在时不能创建私人会话；注销时级联清理。
    """CREATE TABLE research.conversations (
        conversation_id uuid PRIMARY KEY,
        user_id uuid NOT NULL REFERENCES public.app_users(user_id) ON DELETE CASCADE,
        title varchar(160) NOT NULL CHECK (length(btrim(title)) > 0),
        revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
        next_sequence bigint NOT NULL DEFAULT 1 CHECK (next_sequence > 0),
        status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK (updated_at >= created_at)
    )""",
    "CREATE INDEX ix_memory_conversations_owner ON research.conversations(user_id, updated_at DESC, conversation_id DESC)",
    """CREATE TABLE research.conversation_turns (
        turn_id uuid PRIMARY KEY,
        conversation_id uuid NOT NULL REFERENCES research.conversations(conversation_id) ON DELETE CASCADE,
        client_request_id uuid NOT NULL,
        base_revision bigint NOT NULL CHECK (base_revision > 0),
        request_revision bigint NOT NULL CHECK (request_revision = base_revision + 1),
        status varchar(16) NOT NULL CHECK (status IN ('queued','running','completed','stopped','failed')),
        stop_reason varchar(32), error_code varchar(64),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
        UNIQUE(conversation_id, client_request_id),
        UNIQUE(conversation_id, request_revision), UNIQUE(conversation_id, turn_id),
        CHECK ((status IN ('completed','stopped','failed')) = (finished_at IS NOT NULL)),
        CHECK ((status = 'stopped') = (stop_reason IS NOT NULL)),
        CHECK (stop_reason IS NULL OR stop_reason IN ('user_stop','navigation','superseded','connection_lost','server_restart')),
        CHECK ((status = 'failed') = (error_code IS NOT NULL)),
        CHECK (error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
        CHECK (updated_at >= created_at),
        CHECK (finished_at IS NULL OR (finished_at >= created_at AND finished_at <= updated_at))
    )""",
    # 即使绕过 Python，数据库仍保证一个会话最多一条排队/运行轮次。
    "CREATE UNIQUE INDEX ux_memory_active_turn ON research.conversation_turns(conversation_id) WHERE status IN ('queued','running')",
    """CREATE TABLE research.conversation_messages (
        message_id uuid PRIMARY KEY, conversation_id uuid NOT NULL, turn_id uuid NOT NULL,
        sequence bigint NOT NULL CHECK (sequence > 0),
        role varchar(16) NOT NULL CHECK (role IN ('user','assistant')),
        status varchar(16) NOT NULL CHECK (status IN ('queued','running','completed','stopped','failed')),
        content text NOT NULL CHECK (length(content) <= 100000),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(conversation_id, sequence), UNIQUE(turn_id, role),
        UNIQUE(conversation_id, message_id, sequence),
        FOREIGN KEY(conversation_id, turn_id) REFERENCES research.conversation_turns(conversation_id, turn_id) ON DELETE CASCADE,
        CHECK (role <> 'user' OR status = 'completed'),
        CHECK (status <> 'completed' OR content ~ '[^[:space:]]'),
        CHECK (updated_at >= created_at)
    )""",
    """CREATE TABLE research.conversation_summaries (
        summary_id uuid PRIMARY KEY,
        conversation_id uuid NOT NULL REFERENCES research.conversations(conversation_id) ON DELETE CASCADE,
        version bigint NOT NULL CHECK (version > 0),
        based_on_version bigint NOT NULL CHECK (based_on_version >= 0 AND version = based_on_version + 1),
        covered_until_message_id uuid NOT NULL,
        covered_until_sequence bigint NOT NULL CHECK (covered_until_sequence > 0),
        content text NOT NULL CHECK (length(content) <= 100000 AND content ~ '[^[:space:]]'),
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id, version),
        FOREIGN KEY(conversation_id, covered_until_message_id, covered_until_sequence)
            REFERENCES research.conversation_messages(conversation_id, message_id, sequence) ON DELETE CASCADE
    )""",
)


def upgrade():
    """在迁移入口持有的事务内建表和索引；任何语句失败都会整体回滚。"""
    for statement in DDL:
        op.execute(statement)


def downgrade():
    """仅允许显式测试 profile 回退本迁移，逐表删除，不使用 schema CASCADE。"""
    if context.config.attributes.get("active_profile") != "test":
        raise ValueError("Memory 破坏性回退只允许测试库")
    for name in (
        "conversation_summaries",
        "conversation_messages",
        "conversation_turns",
        "conversations",
    ):
        op.execute(f"DROP TABLE research.{name}")
