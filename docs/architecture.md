# 项目目录与模块职责

更新日期：2026-10-19。本文描述当前代码；命令见 [README](../README.md)，操作步骤见 [运行手册](operations.md)。

## 目录

人工维护文件按类型放置，Python 子目录中直接放文件，不再增加业务子目录。

| 位置 | 内容 |
| --- | --- |
| `python/chainscope` | CLI、采集业务、Agent 和数据库访问代码 |
| `python/tests` | 模拟测试、临时 MySQL 测试和可选真实模型检查 |
| `python/tools` | 聊天入口、环境检查、测量和手动采集等辅助入口 |
| `python/migration_env` | Alembic 执行入口 `env.py` |
| `python/migrations` | 保持连续的迁移版本 `0001`～`0009` |
| `python/webapp` | 网站后端（开发中），不导入采集代码，按职责分文件夹：`core`（配置、密钥、错误、邮箱格式、发信）、`db`（账户库连接与表定义）、`security`（密码、令牌、CSRF）、`services`（业务逻辑）、`api`（网页接口）。只允许 `api → services → security/db → core` 方向调用 |
| `python/webapp/migrations` | 账户库（PostgreSQL）的 Alembic 入口 `env.py`，版本在 `versions/`；与 MySQL 迁移链、版本表分开，命令为 `alembic -n webapp` |
| `python/tests/webapp` | 网站后端测试，文件名以 `test_web_` 开头 |
| `config` | 代币服务列表、本地身份和地址标签 |
| `docs` | 当前说明及注明日期的历史设计、学习和验证记录 |
| `sql` | Navicat 查询及历史初始迁移 SQL 导出 |
| `samples` | 固定 CSV 样本 |
| `reports` | 样本来源和专项结果；Agent 结束报告主要在 MySQL |
| `raw/<batch-id>` | 可选调试响应和历史证据，保留数据库引用的路径 |
| `logs` | CLI 或后台进程输出，不代替数据库进度 |
| `.run` | PyCharm 共享运行配置 |
| `.venv`、`*.egg-info` | 安装工具管理的环境和元信息，不是手写源码 |
| `build`（若存在） | 构建或旧临时产物，不作为运行入口。2026-10-23 核对后已删除旧目录（安装元信息副本、旧目录结构占位、旧 IDE 配置、首次发布时的临时检出），均无独有资料；以后再生成时清理前同样先核对 |

根目录保留环境、依赖、迁移和协作工具所需配置。环境、缓存、构建目录和安装元信息从 IDE 日常源码搜索中排除即可，不通过移动解释器整理目录。

## 当前采集流程

日常任务由 `hybrid.sync_hybrid` 执行：Alchemy 按窗口采集和正式提交 Transfer，SQD 独立发现配置范围的池子、补充 Swap。MySQL 保存进度、分页断点、失败窗口和持仓缓存。一轮运行固定截止位置，不自动追随运行后新增区块。

聊天入口是 `python/tools/chat_collection.py`，也可运行 `python -B -m chainscope.agent_runtime`。OpenAI Agents SDK 处理对话与工具调用，DeepSeek 提供模型；独立后台进程执行采集，结束后保存事件并通知聊天入口生成报告。

| 模块 | 职责 |
| --- | --- |
| `agent_runtime.py`、`agent_console.py` | 模型和对话调度、单一输入读取、异步显示 |
| `agent_contracts.py`、`agent_tools.py` | Pydantic 合同、JSON Schema 生成、工具注册和调用适配 |
| `agent_intent.py`、`token_registry.py` | 从用户原话绑定范围和截止条件，读取代币服务列表及策略 |
| `agent_service.py`、`agent_backend.py` | 准备、启动、查询、停止等规则；检查目标并接入既有采集 |
| `agent_models.py`、`agent_store.py` | 四张 Agent 控制表定义，以及计划、运行、尝试和授权的事务操作 |
| `agent_worker.py` | 后台采集、心跳、预算、安全停止和最终结果保存 |
| `agent_notifications.py` | 结束事件、历史查询、报告处理权、本地进程通信和退出监听 |
| `agent_reports.py` | 按绑定事件生成报告，核对身份与状态，附上快照事实 |

五个采集工具为 `get_collection_status`、`prepare_collection`、`start_collection`、`get_collection_run`、`stop_collection`；另有只读的 `list_collection_runs` 和 `get_collection_event`。12 个 `agent_*.py` 是职责模块，不代表同时运行 12 个模型。

明确采集指令在服务列表和既定预算内直接执行；查询、预览不会授权启动。明确时刻先锁定时间边界，再定位区块；转账、Swap、池子和持仓四项覆盖全部达到目标才记录 `completed`。`你 >` 表示输入仍可用，后台结束报告可在没有新输入时显示。

## 原有业务模块

| 模块 | 职责 |
| --- | --- |
| `__main__.py`、`config.py`、`errors.py` | CLI 分派、私密配置和安全错误 |
| `rpc_client.py`、`collector.py`、`parser.py`、`validator.py` | RPC、Alchemy 小范围采集、字段解析和回执核验 |
| `pipeline.py`、`archive.py`、`records.py` | 小范围批次编排、显式调试归档、历史 CSV 检查 |
| `models.py`、`database.py` | 业务表定义、连接、显式迁移、批次事务和读回核对 |
| `jobs.py`、`ingestion.py`、`sync.py`、`hybrid.py` | 持久化任务、分页暂存、连续水位和主采流程 |
| `sqd.py`、`headers.py`、`dex.py`、`supplement.py` | SQD、区块头缓存、池子发现和独立 Swap 补充 |
| `transaction_checks.py`、`reconcile.py`、`acceptance.py` | 窗口回执检查、跨源复查和有限范围验收 |
| `balances.py`、`holders.py`、`query.py`、`market.py` | 持仓缓存、排行、范围查询及行情等已有 CLI 功能 |
| `recovery.py`、`formal_recovery.py` | 显式核验或修复；采集 Agent 不开放这些操作 |

`collect` 是早期小范围批次工具，其事件不属于长期任务；日常增量使用 `sync` 或采集 Agent。行情、研究分析和长期 memory 尚未接入 Agent。

## 保存与验证边界

默认采集持久化到 MySQL，不按事件生成 JSON。`raw` 用于历史证据或显式调试；`reports` 保存专项结果；Agent 进度、结束快照和报告正文存放在控制表中。它们都不代替数据库备份。

`config.ROOT` 根据源码位置找到项目根目录。打包从 `python` 加载业务包，Alembic 使用并列的迁移入口和版本目录。本地身份和数据库引用的归档路径需要保留。

已实现持久化任务、分页恢复、连续水位、独立 Swap 进度、后台执行和结束报告。2026-10-19 已完成一次 BEM 增量到明确时刻的真实闭环，证据见 README。模拟、临时 MySQL 和正式运行分别记录；这不自动证明全部代币、DEX、外部来源完整性或长期持续运行已验收。
