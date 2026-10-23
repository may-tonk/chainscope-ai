# 数据库迁移

更新日期：2026-10-19。当前最新版本为 `0009`。MySQL 表结构由 Alembic 管理，Navicat 用于查看和核对。

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

## 保留与兼容

- `models.py` 与 `agent_models.py` 描述当前期望结构，改 Python 类型定义不会自动改变数据库。
- 已执行版本保留原样，结构变化新增迁移；破坏性 downgrade 被禁用。本轮清理不合并迁移。
- 原始 uint256 金额使用规范字符串完整保存；持仓缓存的 `DECIMAL(65,0)` 有独立范围限制，超限拒绝累计。
- `0008` 仍可使用原 CLI 采集和只读进度检查；完整 Agent 要求 `0009` 和四张控制表。
- `sql/migration-0001.sql` 是历史初始导出，不是当前完整建库入口。

升级恢复点、结构核对和历史数据验证分别记录。测试使用随机临时库，不通过降级正式库做迁移实验，见 [MySQL 测试](mysql-tests.md)。
