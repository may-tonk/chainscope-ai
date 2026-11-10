# 项目目录与模块职责

更新日期：2026-11-10（更新研究主 LLM、配置与四类数据工具；采集业务说明沿用原基线）。命令见 [README](../README.md)，操作步骤见 [运行手册](operations.md)。

## 目录

人工维护文件按职责和既有文件类型约定组织。采集包 `python/chainscope/` 保持平铺；网站和新研究包按职责分层。2026-11-07 用户批准先创建研究目录骨架，下面标为“预留”的位置尚无业务实现。

| 位置 | 内容 |
| --- | --- |
| `python/chainscope` | CLI、采集业务、Agent 和数据库访问代码 |
| `python/chainscope_research` | 独立研究包：已有主 LLM 单轮决策/流式正文、YAML 配置与提示词、四类只读数据工具；专业 subagent、网站研究接口与持久任务未接通 |
| `python/webapp` | 网站后端位置；已有认证源码在 `feat/auth-v1` 分支，新增研究接口沿用该入口，不能把当前分支缺少源码理解为功能被删除 |
| `python/tests` | 模拟测试、临时 MySQL 测试和可选真实模型检查 |
| `python/tests/research` | 研究合同、主 LLM、配置、工具执行与失败边界测试，使用 `test_research_` 前缀 |
| `python/tools` | 聊天入口、环境检查、测量和手动采集等辅助入口 |
| `python/migration_env` | Alembic 执行入口 `env.py` |
| `python/migrations` | 保持连续的迁移版本 `0001`～`0009` |
| `config` | 代币服务列表、本地身份、地址标签，以及研究模型/运行参数/工具策略/提示词 YAML 登记表 |
| `prompts` | 独立 `.txt` 正文；主 Agent 与工具描述统一由 YAML 登记加载，工具描述仍各自保留独立目录；不创建空 subagent 提示词 |
| `deploy` | API、worker 和 Redis 的部署模板预留；不存秘密或运行数据，本轮不创建虚假的可运行配置 |
| `frontend/src` | 沿用认证分支的 app、shared、features 布局；研究、报告、地址和方法功能目录已预留，尚未实现 |
| `docs` | 当前说明及注明日期的历史设计、学习和验证记录 |
| `sql` | Navicat 查询及历史初始迁移 SQL 导出 |
| `samples` | 固定 CSV 样本 |
| `reports` | 样本来源和专项结果；Agent 结束报告主要在 MySQL |
| `raw/<batch-id>` | 可选调试响应和历史证据，保留数据库引用的路径 |
| `logs` | CLI 或后台进程输出，不代替数据库进度 |
| `.run` | PyCharm 共享运行配置 |
| `.venv`、`*.egg-info` | 安装工具管理的环境和元信息，不是手写源码 |
| `build`（若存在） | 构建或旧临时产物；清理前核对独有资料，不作为运行入口 |

根目录保留环境、依赖、迁移和协作工具所需配置。环境、缓存、构建目录和安装元信息从 IDE 日常源码搜索中排除即可，不通过移动解释器整理目录。

## 研究分析目录（骨架始于 2026-11-07）

以下目录已创建，功能文件按后续实施逐步增加。Python 包的 `__init__.py` 只说明用途，不创建模型或连接；其他空目录用 `.gitkeep` 保留。目录存在不表示对应功能已完成。

| 研究包内的位置 | 单一职责 |
| --- | --- |
| `core/` | 研究配置、路径、公共错误和脱敏日志 |
| `contracts/` | 研究请求、指标结果、报告版本、任务状态与流式事件的共享格式 |
| `model/` | 聊天、向量和重排模型的创建与适配 |
| `agent/` | 研究图的组装、状态、可信上下文、运行和中间件 |
| `agent/nodes/` | 澄清、规划、覆盖检查、分析、校验和回答等图节点 |
| `agent/tools/` | 给模型调用的薄接口，不重复实现业务或指标公式 |
| `services/` | 清洗批次、指标查询、地址、对话、报告和任务的业务规则 |
| `analysis/cleaning/` | 事件校验、转账分类、交易合并与归属、结果对账 |
| `analysis/metrics/` | 指标登记、供应、资金流、持仓和时段比较；口径在此统一维护 |
| `knowledge/` | 文档解析与入库、检索、重排及来源引用 |
| `storage/` | MySQL、研究 PostgreSQL、Redis、checkpoint、向量与对象存储的连接和访问适配 |
| `storage/repositories/` | 链上事实、派生结果、会话、报告、任务与地址标签的数据访问 |
| `tasks/` | 后台任务与 worker 入口；目录创建不会注册任务或启动进程 |
| `migrations/versions/` | 研究业务迁移预留，目标数据库、权限和迁移合同确认后再编写；不替代现有 MySQL 或账户迁移链 |

`application.py` 已提供主 Agent 流入口及分析服务公开类型，`bootstrap.py` 负责资源组装。网站沿 `api → services → chainscope_research.application` 方向接入；研究包不反向依赖网站。分析服务构造必须显式注入服务端权限解析器和分页签名密钥，不在启动时迁移或建表。

前端沿用 `app/layouts`、`shared/{api,format,styles,ui,validation}` 和 `features`；已有 `auth`、`account`、`workspace` 的代码以认证分支为准，新增 `research`、`reports`、`addresses`、`methods` 分别预留页面和组件。两大产品模块仍是独立的“AI 研究”和“我的报告”。

本地入口继续放 `python/tools/`；研究配置放 `config/research_*.yaml`。主 LLM 配置由 `core/config_schema.py` 定义、`core/settings.py` 加载；工具策略由 `core/tool_settings.py` 定义与加载。类型均由 Pydantic 维护，实际参数来自 YAML；采集已有 TOML 与项目 `pyproject.toml` 保持原用途。完整消息、图状态和报告版本的存储分别设计，不能把缓存或 checkpoint 当作全部业务记录。

主 Agent 的提示词通过 `config/research_prompts.yaml` 登记逻辑名称、相对 TXT 路径、版本与变量名；`core/prompt_loader.py` 加载不可变模板，检查路径与变量并执行单次替换。`bootstrap.py` 按名称装配，不再写死正文路径。主备共享同一份本轮渲染结果，终止事件携带模板版本与已加载内容摘要。文件修改后重新装配生效，当前不支持热更新，也不缓存含用户参数的渲染结果。

四类工具描述也登记在同一 YAML，使用 `tool_<tool_id>` 名称。`agent/tools/catalog.py` 从已加载的同一目录快照取描述，不拼接 TXT 路径；参数 Schema 由 Pydantic 自动生成。`agent/tools/data_tools.py` 按启用状态和可信上下文授权创建真正的 LangChain `StructuredTool` 异步 handler，调用 `services/data_tools.py`；服务层组合 `analysis/cleaning/transfer_rules.py`、`analysis/metrics/activity.py` 与 `storage/repositories/chain_facts.py`。事实访问复用既有 MySQL 表定义，仅执行只读事务，不导入采集执行逻辑。`services/event_cursor.py` 负责签名分页。当前未向主模型自动绑定工具，也未建立分析 subagent 的工具调用循环。

`config/research_tools.yaml` 管理每实例并行度、每时段行数、时段数、证据样例、地址贡献数量和工具时限；修改后重新装配生效。同步 PyMySQL 查询和整数计算在有界线程运行，异步入口不阻塞事件循环；超时不意味着数据库已停止，槽位待线程真实结束才归还。单实例并行上限不能当作跨进程限流或上线容量证据。

`python/tools/check_research_data_tools.py --job-id bem --hours 1` 在本地数据库权限下只读验收四个实际 `StructuredTool.ainvoke`，并以独立 JOIN 和整数计算对照结果；不调用模型、RPC、迁移或重采，也不自动落盘。该本地操作者身份不能用作网站账户授权实现。

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
