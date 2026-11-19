"""会话存储的 SQLAlchemy 列映射；不可变建表约束见 mem0001 迁移。

这里只声明如何访问表，不执行 create_all。采用手写版本化迁移，不从当前映射自动改库。
所有时间保留时区，UUID 不由浏览器选择，sequence 在会话行锁内连续分配。
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

metadata = sa.MetaData(schema="research")

conversations = sa.Table(
    "conversations",
    metadata,
    sa.Column("conversation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("user_id", UUID(as_uuid=True), nullable=False),
    sa.Column("title", sa.String(160), nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("next_sequence", sa.BigInteger, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
turns = sa.Table(
    "conversation_turns",
    metadata,
    sa.Column("turn_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
    sa.Column("client_request_id", UUID(as_uuid=True), nullable=False),
    sa.Column("base_revision", sa.BigInteger, nullable=False),
    sa.Column("request_revision", sa.BigInteger, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("stop_reason", sa.String(32)),
    sa.Column("error_code", sa.String(64)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)
messages = sa.Table(
    "conversation_messages",
    metadata,
    sa.Column("message_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
    sa.Column("turn_id", UUID(as_uuid=True), nullable=False),
    sa.Column("sequence", sa.BigInteger, nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
summaries = sa.Table(
    "conversation_summaries",
    metadata,
    sa.Column("summary_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
    sa.Column("version", sa.BigInteger, nullable=False),
    sa.Column("based_on_version", sa.BigInteger, nullable=False),
    sa.Column("covered_until_message_id", UUID(as_uuid=True), nullable=False),
    sa.Column("covered_until_sequence", sa.BigInteger, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
