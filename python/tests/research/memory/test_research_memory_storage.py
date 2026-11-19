"""会话存储的真实 PostgreSQL 验证，覆盖事务、并发、隔离和级联清理。

不调用模型或 Qdrant，仅操作测试夹具的随机账号；开发库和已有账号不参与样例。
"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from chainscope_research.memory.contracts.conversations import SendMessageInput
from chainscope_research.memory.storage.conversations import (
    ConversationStore,
    MemoryStoreError,
)
from chainscope_research.memory.storage.database import migration_config


def request(conversation, *, content="用户原文", client_request_id=None, revision=None):
    """构造来自当前会话快照的新消息请求；测试使用固定原文检查幂等与原文保存。"""
    return SendMessageInput(
        conversation_id=conversation.conversation_id,
        client_request_id=client_request_id or uuid4(),
        expected_revision=revision or conversation.revision,
        content=content,
    )


def running(store, user):
    """创建并启动一轮虚构生成，返回接收结果，后续只模拟正文存储。"""
    accepted = store.accept_message(
        user, request(store.create_conversation(user, "测试会话"))
    )
    store.mark_running(
        user,
        accepted.turn.conversation_id,
        accepted.turn.turn_id,
        accepted.turn.request_revision,
    )
    return accepted


def test_create_read_owner_isolation_and_message_cursor(memory_store):
    """账号只能列出自己的会话，跨用户读取和发送统一返回 NOT_FOUND。"""
    store, user, other, _ = memory_store
    conversation = store.create_conversation(user, "会话一")
    accepted = store.accept_message(user, request(conversation, content="  原文\n"))
    assert accepted.user_message.content == "  原文\n"
    assert [
        m.sequence for m in store.list_messages(user, conversation.conversation_id)
    ] == [1, 2]
    assert (
        len(
            store.list_messages(
                user, conversation.conversation_id, after_sequence=1, limit=1
            )
        )
        == 1
    )
    assert len(store.list_conversations(user)) == 1
    assert store.list_conversations(other) == ()
    for operation in (
        lambda: store.get_conversation(other, conversation.conversation_id),
        lambda: store.list_messages(other, conversation.conversation_id),
        lambda: store.accept_message(other, request(conversation)),
    ):
        with pytest.raises(MemoryStoreError, match="NOT_FOUND"):
            operation()


def test_send_replay_and_conflicting_request_key(memory_store):
    """重放旧修订的相同请求返回原轮次；同键不同原文被拒绝，不重复插入。"""
    store, user, _, _ = memory_store
    conversation = store.create_conversation(user, "幂等")
    message = request(conversation)
    first = store.accept_message(user, message)
    replay = store.accept_message(user, message)
    assert first.turn.turn_id == replay.turn.turn_id and replay.replayed
    with pytest.raises(MemoryStoreError, match="IDEMPOTENCY_CONFLICT"):
        store.accept_message(
            user,
            request(
                conversation,
                client_request_id=message.client_request_id,
                content="被更改的原文",
            ),
        )
    assert len(store.list_messages(user, conversation.conversation_id)) == 2


def test_concurrent_identical_sends_create_one_turn(memory_store):
    """两个真实数据库连接并发重放，只接受一次并返回同一轮次。"""
    store, user, _, _ = memory_store
    message = request(store.create_conversation(user, "并发重放"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: store.accept_message(user, message), range(2))
        )
    assert results[0].turn.turn_id == results[1].turn.turn_id
    assert sorted(result.replayed for result in results) == [False, True]
    assert len(store.list_messages(user, message.conversation_id)) == 2


def test_concurrent_different_sends_compare_revision(memory_store):
    """同一旧修订并发提交不同请求时只有一个成功，另一个得到修订冲突。"""
    store, user, _, _ = memory_store
    conversation = store.create_conversation(user, "并发修订")

    def send(index):
        """在独立连接提交并返回稳定结果，测试不吞掉其他数据库故障。"""
        try:
            return store.accept_message(user, request(conversation, content=str(index)))
        except MemoryStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert sum(result == "REVISION_CONFLICT" for result in results) == 1
    assert len(store.list_messages(user, conversation.conversation_id)) == 2


def test_chunk_retry_and_terminal_state_survive_new_connection(memory_store):
    """带 Unicode 的片段可幂等重放，结束后经新连接读取仍保留完整正文。"""
    store, user, _, _ = memory_store
    accepted = running(store, user)
    args = (
        user,
        accepted.turn.conversation_id,
        accepted.turn.turn_id,
        accepted.turn.request_revision,
    )
    store.append_reply(*args, expected_length=0, text="半截🔍")
    store.append_reply(*args, expected_length=0, text="半截🔍")
    with pytest.raises(MemoryStoreError, match="CHUNK_CONFLICT"):
        store.append_reply(*args, expected_length=99, text="错误片段")
    store.append_reply(*args, expected_length=len("半截🔍"), text="正文")
    store.finish_turn(*args, status="completed")
    store.finish_turn(*args, status="completed")
    # 新连接池确保结果来自已提交事务，而非原连接尚未提交的状态。
    with store.engine.connect() as connection:
        read = connection.execute(
            sa.text(
                "SELECT content,status FROM research.conversation_messages WHERE turn_id=:id AND role='assistant'"
            ),
            {"id": accepted.turn.turn_id},
        ).one()
        assert tuple(read) == ("半截🔍正文", "completed")
    assert ConversationStore(store.engine).get_turn(*args[:3]).status == "completed"
    with pytest.raises(MemoryStoreError, match="TERMINAL_CONFLICT"):
        store.finish_turn(*args, status="failed", error_code="TIMEOUT")


def test_superseded_turn_cannot_write_over_new_turn(memory_store):
    """新消息原子停止旧轮并保留片段，旧 Worker 后续写入被拒绝。"""
    store, user, _, _ = memory_store
    old = running(store, user)
    args = (user, old.turn.conversation_id, old.turn.turn_id, old.turn.request_revision)
    store.append_reply(*args, expected_length=0, text="旧回复半截")
    current = store.get_conversation(user, old.turn.conversation_id)
    new = store.accept_message(user, request(current, content="新的提问"))
    stopped = store.get_turn(user, old.turn.conversation_id, old.turn.turn_id)
    assert stopped.status == "stopped" and stopped.stop_reason == "superseded"
    with pytest.raises(MemoryStoreError, match="STALE_TURN"):
        store.append_reply(*args, expected_length=5, text="迟到内容")
    assert new.turn.status == "queued"
    assert [m.sequence for m in store.list_messages(user, current.conversation_id)] == [
        1,
        2,
        3,
        4,
    ]


def test_invalid_finish_rolls_back_and_stop_preserves_partial(memory_store):
    """空成功或缺少停止原因时事务不改变状态；合法停止保存半截内容。"""
    store, user, _, _ = memory_store
    accepted = running(store, user)
    args = (
        user,
        accepted.turn.conversation_id,
        accepted.turn.turn_id,
        accepted.turn.request_revision,
    )
    with pytest.raises(ValidationError):
        store.finish_turn(*args, status="completed")
    with pytest.raises(ValidationError):
        store.finish_turn(*args, status="stopped")
    assert store.get_turn(*args[:3]).status == "running"
    store.append_reply(*args, expected_length=0, text="停止前片段")
    store.finish_turn(*args, status="stopped", stop_reason="navigation")
    assistant = store.list_messages(user, accepted.turn.conversation_id)[1]
    assert assistant.status == "stopped" and assistant.content == "停止前片段"


def test_database_rejects_cross_conversation_message_binding(memory_store):
    """数据库复合外键也拒绝错误会话与轮次组合，验证不是仅依赖 Python 校验。"""
    store, user, _, migrator = memory_store
    accepted = running(store, user)
    foreign = store.create_conversation(user, "另一会话")
    with migrator.begin() as connection:
        # 预置不带消息的终态轮次，避免先触发 turn_id/role 唯一约束掩盖外键错误。
        detached_turn = uuid4()
        connection.execute(
            sa.text("""
            INSERT INTO research.conversation_turns
                (turn_id,conversation_id,client_request_id,base_revision,request_revision,status,error_code,finished_at)
            VALUES (:id,:conversation,:request,99,100,'failed','TEST_ONLY',now())
        """),
            {
                "id": detached_turn,
                "conversation": accepted.turn.conversation_id,
                "request": uuid4(),
            },
        )
        with pytest.raises(IntegrityError) as captured, connection.begin_nested():
            connection.execute(
                sa.text("""
                INSERT INTO research.conversation_messages
                    (message_id,conversation_id,turn_id,sequence,role,status,content)
                VALUES (:id,:conversation,:turn,9,'assistant','running','wrong')
            """),
                {
                    "id": uuid4(),
                    "conversation": foreign.conversation_id,
                    "turn": detached_turn,
                },
            )
        assert captured.value.orig.sqlstate == "23503"


def test_delete_only_owned_conversation_and_cascades_children(memory_store):
    """删除必须校验用户与修订，成功后级联清理消息、轮次和摘要。"""
    store, user, other, migrator = memory_store
    accepted = running(store, user)
    conversation_id = accepted.turn.conversation_id
    with migrator.begin() as connection:
        connection.execute(
            sa.text("""
            INSERT INTO research.conversation_summaries
                (summary_id,conversation_id,version,based_on_version,covered_until_message_id,covered_until_sequence,content)
            VALUES (:id,:conversation,1,0,:message,1,'test summary')
        """),
            {
                "id": uuid4(),
                "conversation": conversation_id,
                "message": accepted.user_message.message_id,
            },
        )
    with pytest.raises(MemoryStoreError, match="NOT_FOUND"):
        store.delete_conversation(
            other, conversation_id, expected_revision=accepted.conversation.revision
        )
    with pytest.raises(MemoryStoreError, match="REVISION_CONFLICT"):
        store.delete_conversation(user, conversation_id, expected_revision=1)
    store.delete_conversation(
        user, conversation_id, expected_revision=accepted.conversation.revision
    )
    with migrator.connect() as connection:
        for table in (
            "conversations",
            "conversation_turns",
            "conversation_messages",
            "conversation_summaries",
        ):
            assert (
                connection.scalar(
                    sa.text(
                        f"SELECT count(*) FROM research.{table} WHERE conversation_id=:id"
                    ),
                    {"id": conversation_id},
                )
                == 0
            )


def test_memory_migration_respects_caller_transaction(memory_engines):
    """在外部事务中回退空的测试会话结构，再回滚；新连接应仍看到原版本与表。

    证明迁移入口不擅自提交，也不把检查 SELECT 导致的隐式事务误当成已提交。
    """
    migrator, _ = memory_engines
    with migrator.connect() as connection:
        transaction = connection.begin()
        try:
            config = migration_config("test")
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
            assert (
                connection.scalar(
                    sa.text("SELECT to_regclass('research.conversations')")
                )
                is None
            )
        finally:
            transaction.rollback()
    with migrator.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT version_num FROM research.memory_alembic_version")
            )
            == "mem0001"
        )
        assert connection.scalar(
            sa.text("SELECT to_regclass('research.conversations') IS NOT NULL")
        )
