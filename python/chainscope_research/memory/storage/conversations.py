"""PostgreSQL 会话存储：用户隔离、幂等发送、流式片段和终态的事务实现。

每次写入先锁定属于当前用户的会话行，统一分配修订号和消息顺序。
模型调用和 SSE 网络等待不得放在这些短事务内；导入和构造存储均不建表。
"""

from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from chainscope_research.memory.contracts.common import MemoryUserContext
from chainscope_research.memory.contracts.conversations import (
    ConversationRecord,
    MessageRecord,
    SendMessageInput,
    TurnAcceptance,
    TurnRecord,
)
from chainscope_research.memory.services.state_rules import ensure_turn_transition
from chainscope_research.memory.settings import load_memory_settings
from chainscope_research.memory.storage.tables import conversations, messages, turns


class MemoryStoreError(Exception):
    """存储边界的稳定错误码，不包含原文、SQL 参数或数据库驱动异常。"""

    def __init__(self, code: str):
        """接收内部稳定错误码，供后续 API 映射状态码；不打印数据库原始原因。"""
        super().__init__(code)
        self.code = code


def _record(model, row):
    """按合同字段提取数据库行，返回已校验快照，排除内部计数列。"""
    return model.model_validate({name: row[name] for name in model.model_fields})


class ConversationStore:
    """注入已验证的 PostgreSQL 运行连接池；池生命周期由装配方负责。"""

    def __init__(self, engine, *, settings=None):
        """保存连接池，不建立连接或执行迁移；仅接受本项目目标 PostgreSQL 方言。"""
        if engine.dialect.name != "postgresql":
            raise ValueError("Memory 会话存储要求 PostgreSQL")
        self.engine = engine
        self.settings = settings or load_memory_settings()

    @contextmanager
    def _transaction(self):
        """开启短事务，正常提交、异常回滚；驱动错误统一转换为安全代码。"""
        try:
            with self.engine.begin() as connection:
                yield connection
        except SQLAlchemyError:
            raise MemoryStoreError("DATABASE_FAILURE") from None

    def _conversation(self, connection, context, conversation_id, *, lock=False):
        """只取当前用户的有效会话；不存在与他人资源返回同一种错误，避免泄漏。"""
        query = sa.select(conversations).where(
            conversations.c.conversation_id == conversation_id,
            conversations.c.user_id == context.user_id,
            conversations.c.status == "active",
        )
        if lock:
            query = query.with_for_update()
        row = connection.execute(query).mappings().one_or_none()
        if row is None:
            raise MemoryStoreError("NOT_FOUND")
        return row

    def _turn(self, connection, conversation_id, turn_id):
        """在已确认归属的会话内取轮次，防止跨会话拼接 turn_id。"""
        row = (
            connection.execute(
                sa.select(turns).where(
                    turns.c.conversation_id == conversation_id,
                    turns.c.turn_id == turn_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise MemoryStoreError("NOT_FOUND")
        return row

    def _acceptance(self, connection, conversation, turn, *, replayed):
        """在同一事务中组装轮次和两条消息；不返回内部游标、连接或可修改行。"""
        rows = (
            connection.execute(
                sa.select(messages).where(
                    messages.c.turn_id == turn["turn_id"],
                )
            )
            .mappings()
            .all()
        )
        by_role = {row["role"]: _record(MessageRecord, row) for row in rows}
        return TurnAcceptance(
            conversation=_record(ConversationRecord, conversation),
            turn=_record(TurnRecord, turn),
            user_message=by_role["user"],
            assistant_message=by_role["assistant"],
            replayed=replayed,
        )

    def create_conversation(
        self, context: MemoryUserContext, title: str
    ) -> ConversationRecord:
        """为可信用户创建会话并提交，返回快照；账号必须已存在，标题不允许空白。"""
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
            raise ValueError("会话标题需要 1 到 160 个字符")
        with self._transaction() as connection:
            row = (
                connection.execute(
                    sa.insert(conversations)
                    .values(
                        conversation_id=uuid4(),
                        user_id=context.user_id,
                        title=title.strip(),
                    )
                    .returning(conversations)
                )
                .mappings()
                .one()
            )
            return _record(ConversationRecord, row)

    def get_conversation(self, context, conversation_id: UUID) -> ConversationRecord:
        """按用户归属读取一条会话；不会查询或返回其他用户的资源信息。"""
        with self._transaction() as connection:
            return _record(
                ConversationRecord,
                self._conversation(connection, context, conversation_id),
            )

    def list_conversations(
        self, context, *, limit=None, before: tuple[datetime, UUID] | None = None
    ):
        """按更新时间和 UUID 倒序游标分页；before 使用上一页末条的时间与 ID。"""
        limit = self.settings.history.page_size if limit is None else limit
        if (
            type(limit) is not int
            or not 1 <= limit <= self.settings.history.max_page_size
        ):
            raise ValueError("会话分页大小超出 Memory 配置")
        query = sa.select(conversations).where(
            conversations.c.user_id == context.user_id,
            conversations.c.status == "active",
        )
        if before is not None:
            if before[0].utcoffset() is None:
                raise ValueError("分页时间需要时区")
            query = query.where(
                sa.tuple_(conversations.c.updated_at, conversations.c.conversation_id)
                < sa.tuple_(*before)
            )
        with self._transaction() as connection:
            rows = connection.execute(
                query.order_by(
                    conversations.c.updated_at.desc(),
                    conversations.c.conversation_id.desc(),
                ).limit(limit)
            ).mappings()
            return tuple(_record(ConversationRecord, row) for row in rows)

    def list_messages(
        self, context, conversation_id: UUID, *, after_sequence=0, limit=None
    ):
        """校验会话归属后按稳定 sequence 分页读取原文，保留半截回复和状态。"""
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("消息游标必须是非负整数")
        limit = self.settings.history.page_size if limit is None else limit
        if (
            type(limit) is not int
            or not 1 <= limit <= self.settings.history.max_page_size
        ):
            raise ValueError("消息分页大小超出 Memory 配置")
        with self._transaction() as connection:
            self._conversation(connection, context, conversation_id)
            rows = connection.execute(
                sa.select(messages)
                .where(
                    messages.c.conversation_id == conversation_id,
                    messages.c.sequence > after_sequence,
                )
                .order_by(messages.c.sequence)
                .limit(limit)
            ).mappings()
            return tuple(_record(MessageRecord, row) for row in rows)

    def accept_message(self, context, request: SendMessageInput) -> TurnAcceptance:
        """原子接收用户消息及助手占位，递增修订号；相同请求重放不重复生成。

        幂等查找先于修订号检查；复用请求 ID 却改变正文或基础版本时拒绝。
        新消息在同一事务中停止旧轮，旧 Worker 之后不能继续写入旧流。
        """
        with self._transaction() as connection:
            conversation = self._conversation(
                connection, context, request.conversation_id, lock=True
            )
            previous = (
                connection.execute(
                    sa.select(turns).where(
                        turns.c.conversation_id == request.conversation_id,
                        turns.c.client_request_id == request.client_request_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                original = connection.scalar(
                    sa.select(messages.c.content).where(
                        messages.c.turn_id == previous["turn_id"],
                        messages.c.role == "user",
                    )
                )
                if (
                    original != request.content
                    or previous["base_revision"] != request.expected_revision
                ):
                    raise MemoryStoreError("IDEMPOTENCY_CONFLICT")
                return self._acceptance(
                    connection, conversation, previous, replayed=True
                )
            if conversation["revision"] != request.expected_revision:
                raise MemoryStoreError("REVISION_CONFLICT")
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            old_ids = (
                connection.execute(
                    sa.select(turns.c.turn_id).where(
                        turns.c.conversation_id == request.conversation_id,
                        turns.c.status.in_(("queued", "running")),
                    )
                )
                .scalars()
                .all()
            )
            if old_ids:
                connection.execute(
                    sa.update(turns)
                    .where(turns.c.turn_id.in_(old_ids))
                    .values(
                        status="stopped",
                        stop_reason="superseded",
                        finished_at=now,
                        updated_at=now,
                    )
                )
                connection.execute(
                    sa.update(messages)
                    .where(
                        messages.c.turn_id.in_(old_ids),
                        messages.c.role == "assistant",
                    )
                    .values(status="stopped", updated_at=now)
                )
            revision = conversation["revision"] + 1
            sequence = conversation["next_sequence"]
            turn = (
                connection.execute(
                    sa.insert(turns)
                    .values(
                        turn_id=uuid4(),
                        conversation_id=request.conversation_id,
                        client_request_id=request.client_request_id,
                        base_revision=request.expected_revision,
                        request_revision=revision,
                        status="queued",
                        created_at=now,
                        updated_at=now,
                    )
                    .returning(turns)
                )
                .mappings()
                .one()
            )
            connection.execute(
                sa.insert(messages),
                [
                    dict(
                        message_id=uuid4(),
                        conversation_id=request.conversation_id,
                        turn_id=turn["turn_id"],
                        sequence=sequence,
                        role="user",
                        status="completed",
                        content=request.content,
                        created_at=now,
                        updated_at=now,
                    ),
                    dict(
                        message_id=uuid4(),
                        conversation_id=request.conversation_id,
                        turn_id=turn["turn_id"],
                        sequence=sequence + 1,
                        role="assistant",
                        status="queued",
                        content="",
                        created_at=now,
                        updated_at=now,
                    ),
                ],
            )
            conversation = (
                connection.execute(
                    sa.update(conversations)
                    .where(
                        conversations.c.conversation_id == request.conversation_id,
                    )
                    .values(
                        revision=revision, next_sequence=sequence + 2, updated_at=now
                    )
                    .returning(conversations)
                )
                .mappings()
                .one()
            )
            return self._acceptance(connection, conversation, turn, replayed=False)

    def find_request(self, context, request: SendMessageInput):
        """在上下文预算检查前识别已接收请求，重放不会因后续历史增长而失效。"""
        with self._transaction() as connection:
            conversation = self._conversation(
                connection, context, request.conversation_id
            )
            turn = (
                connection.execute(
                    sa.select(turns).where(
                        turns.c.conversation_id == request.conversation_id,
                        turns.c.client_request_id == request.client_request_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if turn is None:
                return None
            original = connection.scalar(
                sa.select(messages.c.content).where(
                    messages.c.turn_id == turn["turn_id"],
                    messages.c.role == "user",
                )
            )
            if (
                original != request.content
                or turn["base_revision"] != request.expected_revision
            ):
                raise MemoryStoreError("IDEMPOTENCY_CONFLICT")
            return self._acceptance(connection, conversation, turn, replayed=True)

    def _active_turn(
        self, connection, context, conversation_id, turn_id, request_revision
    ):
        """锁会话并核对当前轮次修订，拒绝新提问之后迟到的旧 Worker。"""
        conversation = self._conversation(
            connection, context, conversation_id, lock=True
        )
        turn = self._turn(connection, conversation_id, turn_id)
        if (
            conversation["revision"] != request_revision
            or turn["request_revision"] != request_revision
            or turn["status"] not in {"queued", "running"}
        ):
            raise MemoryStoreError("STALE_TURN")
        return turn

    def mark_running(
        self, context, conversation_id, turn_id, request_revision, *, claim=False
    ) -> TurnRecord:
        """将当前排队轮次及助手消息改为 running；重复启动返回原状态。"""
        with self._transaction() as connection:
            turn = self._active_turn(
                connection, context, conversation_id, turn_id, request_revision
            )
            if turn["status"] == "running":
                if claim:
                    raise MemoryStoreError("TURN_ALREADY_RUNNING")
                return _record(TurnRecord, turn)
            ensure_turn_transition(turn["status"], "running")
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                sa.update(messages)
                .where(
                    messages.c.turn_id == turn_id,
                    messages.c.role == "assistant",
                )
                .values(status="running", updated_at=now)
            )
            row = (
                connection.execute(
                    sa.update(turns)
                    .where(turns.c.turn_id == turn_id)
                    .values(
                        status="running",
                        updated_at=now,
                    )
                    .returning(turns)
                )
                .mappings()
                .one()
            )
            return _record(TurnRecord, row)

    def append_reply(
        self,
        context,
        conversation_id,
        turn_id,
        request_revision,
        *,
        expected_length: int,
        text: str,
    ) -> MessageRecord:
        """按已落库字符位置追加一批正文；相同片段重试不重复追加，偏移冲突拒绝。

        expected_length 为服务端 Python 字符数，不由浏览器以 UTF-16 长度推算。
        调用方应按时间或大小批量调用，在提交成功后发送对应 SSE。
        """
        if (
            type(expected_length) is not int
            or expected_length < 0
            or not isinstance(text, str)
            or not text
        ):
            raise ValueError("追加正文需要非负字符位置和非空文本")
        with self._transaction() as connection:
            turn = self._active_turn(
                connection, context, conversation_id, turn_id, request_revision
            )
            if turn["status"] != "running":
                raise MemoryStoreError("TURN_NOT_RUNNING")
            row = (
                connection.execute(
                    sa.select(messages).where(
                        messages.c.turn_id == turn_id,
                        messages.c.role == "assistant",
                    )
                )
                .mappings()
                .one()
            )
            content = row["content"]
            if (
                expected_length < len(content)
                and content[expected_length : expected_length + len(text)] == text
            ):
                return _record(MessageRecord, row)
            if expected_length != len(content):
                raise MemoryStoreError("CHUNK_CONFLICT")
            if len(content) + len(text) > 100000:
                raise ValueError("正文超过存储合同上限")
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            row = (
                connection.execute(
                    sa.update(messages)
                    .where(messages.c.message_id == row["message_id"])
                    .values(
                        content=content + text,
                        updated_at=now,
                    )
                    .returning(messages)
                )
                .mappings()
                .one()
            )
            connection.execute(
                sa.update(conversations)
                .where(
                    conversations.c.conversation_id == conversation_id,
                )
                .values(updated_at=now)
            )
            return _record(MessageRecord, row)

    def finish_turn(
        self,
        context,
        conversation_id,
        turn_id,
        request_revision,
        *,
        status,
        stop_reason=None,
        error_code=None,
    ) -> TurnRecord:
        """原子保存轮次与助手消息终态；保留正文，拒绝空成功和终态改写。"""
        if status not in {"completed", "stopped", "failed"}:
            raise ValueError("结束轮次必须选择终态")
        with self._transaction() as connection:
            conversation = self._conversation(
                connection, context, conversation_id, lock=True
            )
            turn = self._turn(connection, conversation_id, turn_id)
            if turn["request_revision"] != request_revision:
                raise MemoryStoreError("STALE_TURN")
            if turn["status"] in {"completed", "stopped", "failed"}:
                if (turn["status"], turn["stop_reason"], turn["error_code"]) == (
                    status,
                    stop_reason,
                    error_code,
                ):
                    return _record(TurnRecord, turn)
                raise MemoryStoreError("TERMINAL_CONFLICT")
            if conversation["revision"] != request_revision:
                raise MemoryStoreError("STALE_TURN")
            ensure_turn_transition(turn["status"], status)
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            result = TurnRecord.model_validate(
                {
                    **{name: turn[name] for name in TurnRecord.model_fields},
                    "status": status,
                    "stop_reason": stop_reason,
                    "error_code": error_code,
                    "finished_at": now,
                    "updated_at": now,
                }
            )
            assistant = (
                connection.execute(
                    sa.select(messages).where(
                        messages.c.turn_id == turn_id,
                        messages.c.role == "assistant",
                    )
                )
                .mappings()
                .one()
            )
            # 写入前先验证消息和轮次，确保空正文不能被标成成功。
            MessageRecord.model_validate(
                {
                    **{name: assistant[name] for name in MessageRecord.model_fields},
                    "status": status,
                }
            )
            connection.execute(
                sa.update(turns)
                .where(turns.c.turn_id == turn_id)
                .values(
                    status=status,
                    stop_reason=stop_reason,
                    error_code=error_code,
                    finished_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                sa.update(messages)
                .where(messages.c.message_id == assistant["message_id"])
                .values(
                    status=status,
                    updated_at=now,
                )
            )
            connection.execute(
                sa.update(conversations)
                .where(
                    conversations.c.conversation_id == conversation_id,
                )
                .values(updated_at=now)
            )
            return result

    def get_turn(self, context, conversation_id, turn_id) -> TurnRecord:
        """在用户会话范围内读取轮次，便于恢复页面或检查事务结果。"""
        with self._transaction() as connection:
            self._conversation(connection, context, conversation_id)
            return _record(TurnRecord, self._turn(connection, conversation_id, turn_id))

    def delete_conversation(
        self, context, conversation_id, *, expected_revision: int
    ) -> None:
        """核对用户和修订后删除会话，数据库级联清理轮次、消息、摘要。

        本步还没有长期记忆表或 checkpoint 接线；后续来源外键需 SET NULL，
        删除长期记忆和图状态清理由对应业务单独组织。
        """
        with self._transaction() as connection:
            conversation = self._conversation(
                connection, context, conversation_id, lock=True
            )
            if conversation["revision"] != expected_revision:
                raise MemoryStoreError("REVISION_CONFLICT")
            connection.execute(
                sa.delete(conversations).where(
                    conversations.c.conversation_id == conversation_id
                )
            )

    def heartbeat_turn(
        self, context, conversation_id, turn_id, request_revision
    ) -> None:
        """刷新仍属于当前轮次的执行心跳；多实例中被取代的旧轮会被拒绝。"""
        with self._transaction() as connection:
            self._active_turn(
                connection, context, conversation_id, turn_id, request_revision
            )
            connection.execute(
                sa.update(turns)
                .where(turns.c.turn_id == turn_id)
                .values(
                    updated_at=sa.func.clock_timestamp(),
                )
            )

    def recover_stale_turns(self, context, conversation_id) -> None:
        """按配置回收无心跳的轮次，保留已提交正文；仅操作当前用户会话。

        在页面读取或新发送前调用，不根据前端断连直接判断其他实例进程死亡。
        """
        with self._transaction() as connection:
            self._conversation(connection, context, conversation_id, lock=True)
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            from datetime import timedelta

            stale = (
                connection.execute(
                    sa.select(turns.c.turn_id).where(
                        turns.c.conversation_id == conversation_id,
                        turns.c.status.in_(("queued", "running")),
                        turns.c.updated_at
                        < now
                        - timedelta(seconds=self.settings.recovery.stale_after_seconds),
                    )
                )
                .scalars()
                .all()
            )
            if stale:
                connection.execute(
                    sa.update(turns)
                    .where(turns.c.turn_id.in_(stale))
                    .values(
                        status="stopped",
                        stop_reason="server_restart",
                        finished_at=now,
                        updated_at=now,
                    )
                )
                connection.execute(
                    sa.update(messages)
                    .where(
                        messages.c.turn_id.in_(stale),
                        messages.c.role == "assistant",
                    )
                    .values(status="stopped", updated_at=now)
                )
