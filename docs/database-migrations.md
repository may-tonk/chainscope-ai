# 数据库迁移

更新日期：2026-11-16（账户迁移说明与 Memory 基础设施初始化）。当前采集 MySQL 迁移脚本最新为 `0009`，网站 PostgreSQL 账户迁移脚本最新为 `0002`；本次没有执行或修改这两套迁移链。Memory 小节记录实际执行的独立 schema 与 checkpoint 初始化。Navicat 继续用于查看和核对。

## 执行入口

入口为 `python/migration_env/env.py`，版本脚本在并列的 `python/migrations`；路径由 `alembic.ini` 和 CLI 配置。新环境建库或显式升级前先核对目标数据库：

```powershell
.\.venv\Scripts\python.exe -B -m chainscope init-db
.\.venv\Scripts\python.exe -B -m chainscope check
```

`init-db` 是写操作，会创建指定库并沿版本链升级。已经就绪的数据库日常采集无需重复执行，聊天程序也不自动迁移。`check` 只读核对 RPC、数据库连接和结构，`schema_differences` 应为空。

## 版本职责

| 版本 | 主要变更 |
| --- | --- |
| `0001` | 批次、Transfer 和批次事件关联 |
| `0002` | `raw_directory` 可空，支持默认不归档 |
| `0003` | 长期任务、采集窗口、窗口关联和批次任务标识 |
| `0004` | 回执检查记录、金额排序辅助列和差异记录 |
| `0005` | DEX、Swap、持仓、标签及行情结构 |
| `0006` | 部署/终点区块、指纹版本和池子集合等兼容字段 |
| `0007` | `transfer_ingestion`、`pending_transfers`，区分分页暂存与正式数据 |
| `0008` | 区块头缓存、独立补充进度和 Swap 窗口 |
| `0009` | `agent_plans`、`agent_runs`、`agent_attempts`、`agent_events` |

`0009` 只描述新增的四张 Agent 表，不包含前面所有建表步骤。新建库、升级旧环境和部分旧版本恢复后的升级仍需要完整版本链。

## 已实现的网站账户迁移

`alembic.ini` 已有独立 `[webapp]` 配置：入口为 `python/webapp/migrations/env.py`，脚本位于 `python/webapp/migrations/versions/`。`webapp.db.database.migration_config()` 选择该配置；已有 CLI 入口使用 `alembic -n webapp`，不经过采集 MySQL 的 `init-db`。

| 账户版本 | 已有迁移职责 |
| --- | --- |
| `0001` | `app_users`、`auth_challenges`、`auth_sessions`、`auth_rate_limits`、`auth_events` 五张账户/认证表 |
| `0002` | 验证表增加密码重置用途、关联用户、外键/用途约束和索引 |

账户迁移使用独立迁移账号；普通运行账号用于网站日常读写。已实现的迁移入口检查目标库、账号、schema 权限与 TLS，拒绝超级管理员；账户 downgrade 仅允许测试库。网站启动不自动建表或升级，生产账户库装配与部署仍未完成。账户迁移不放入 `python/migrations/0001`～`0009`，也不代表研究会话、报告或任务已有迁移；研究业务迁移仍预留。

## Memory 基础设施初始化

`python/tools/prepare_memory_database.py --apply` 是独立的显式环境准备入口，使用现有 development/test profile 及各自迁移账号，校验目标库、非超级用户和 TLS 后创建 `research`、`research_checkpoint` 两个 schema。已有 schema 的所有者不匹配时拒绝接管。运行账号仅取得 schema USAGE、业务表读写与必要序列权限，不取得 CREATE 权限。

`research` 暂为空，聊天与记忆业务表仍待后续独立 Alembic 迁移；`research_checkpoint` 使用锁定的 `langgraph-checkpoint-postgres 3.2.0` 官方 `PostgresSaver.setup()` 显式初始化，其 `checkpoint_migrations` 与账户 `public.alembic_version` 独立。内部并发索引迁移使用独立 autocommit 连接，不放进聊天业务事务，网站启动不调用 setup。

```powershell
.\.venv\Scripts\python.exe -B python/tools/prepare_memory_database.py --apply
```

2026-11-16 已在开发、测试两套库执行，运行账号的检查点写入、关闭连接后重新读取和测试 thread 清理通过，DDL 权限保持 false。未升级 PostgreSQL（仍为 18.6），未安装 pgvector，未修改 MySQL 原始事实或账户表结构。

## 保留与兼容

- `models.py` 与 `agent_models.py` 描述当前期望结构，改 Python 类型定义不会自动改变数据库。
- 已执行版本保留原样，结构变化新增迁移；采集 MySQL 的破坏性 downgrade 被禁用。采集、账户和预留研究迁移不合并。
- 原始 uint256 金额使用规范字符串完整保存；持仓缓存的 `DECIMAL(65,0)` 有独立范围限制，超限拒绝累计。
- `0008` 仍可使用原 CLI 采集和只读进度检查；完整 Agent 要求 `0009` 和四张控制表。
- `sql/migration-0001.sql` 是历史初始导出，不是当前完整建库入口。

升级恢复点、结构核对和历史数据验证分别记录。测试使用随机临时库，不通过降级正式库做迁移实验，见 [MySQL 测试](mysql-tests.md)。
