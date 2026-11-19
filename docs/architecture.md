# 项目目录与模块职责

更新日期：2026-11-17（Memory 第四步：运行配置与网页持久聊天接线）。命令见 [README](../README.md)，操作步骤见 [运行手册](operations.md)。测试与环境验证的范围分别记录，不代表生产验收完成。

当前状态对应 `feat/research-chat` 分支：研究、网站与聊天源码已经整合，Memory 部署和环境验收工具一并维护。早期文档中注明日期的分支名称与骨架状态属于历史记录。

## 目录

人工维护文件按职责和既有文件类型约定组织。采集包 `python/chainscope/` 保持平铺；网站和新研究包按职责分层。2026-11-07 用户批准先创建研究目录骨架，下面标为“预留”的位置尚无业务实现。

| 位置 | 内容 |
| --- | --- |
| `python/chainscope` | CLI、采集业务、Agent 和数据库访问代码 |
| `python/chainscope_research` | 独立研究包：已有主 LLM 单轮决策/流式正文、YAML 配置与提示词、四类只读数据工具；专业 subagent、网站研究接口与持久任务未接通 |
| `python/webapp` | 已有 FastAPI 账户/会话接口、主 LLM 聊天 SSE 与聊天配置；使用独立 PostgreSQL 账户库，网站研究执行接口仍未接通 |
| `python/tests` | 模拟测试、临时 MySQL 测试和可选真实模型检查 |
| `python/tests/research` | 研究合同、主 LLM、配置、工具执行与失败边界测试，使用 `test_research_` 前缀 |
| `python/tests/webapp` | 认证、账户、迁移与聊天测试，使用 `test_web_` 前缀 |
| `python/tools` | 聊天入口、环境检查、测量和手动采集等辅助入口；Memory 工具统一在 `python/tools/memory/` |
| `python/migration_env` | Alembic 执行入口 `env.py` |
| `python/migrations` | 保持连续的迁移版本 `0001`～`0009` |
| `config` | 代币服务列表、本地身份、地址标签，以及研究模型/运行参数/工具策略/提示词与网页聊天传输 YAML；Memory 配置统一在 `config/memory/` |
| `prompts` | 独立 `.txt` 正文；主 Agent 与工具描述统一由 YAML 登记加载，工具描述仍各自保留独立目录；不创建空 subagent 提示词 |
| `deploy` | Redis 与 Qdrant 的无秘密部署模板；Memory 部署材料统一在 `deploy/memory/`，证书、凭证、运行数据放在仓库外，API 和业务 worker 的生产部署仍待完成 |
| `frontend/src` | 已有 app、shared、features 分层，认证、账户设置、工作台、真实主 LLM 聊天与研究会话组件；报告、地址和方法功能仍预留 |
| `frontend/tests` | 认证、账户、研究入口、聊天流协议与交互测试 |
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

## 研究分析目录与实际职责

目录骨架始于 2026-11-07，现已实现主 LLM、共享合同、四类工具和以下对应文件。Python 包的 `__init__.py` 只说明用途，不创建模型或连接；标为预留的目录仍无对应业务实现。目录存在不表示功能完成。

| 研究包内的位置 | 单一职责 |
| --- | --- |
| `core/` | 研究配置、路径、公共错误和脱敏日志 |
| `contracts/` | 已有研究草稿、对象/时间、工具输入输出、发现/总结草稿、调用关联与主 LLM 流事件的 Pydantic 合同；合同不代替任务或报告实现 |
| `model/` | 已有 DeepSeek 决策/正文适配与有预算的主备切换；向量与重排模型未实现 |
| `agent/` | 已有 `main_graph.py` 的单轮异步决策/校验图和工具薄适配；专业 subagent 调度未实现 |
| `agent/nodes/` | 专业研究节点预留；当前实际决策与校验节点在 `agent/main_graph.py` |
| `agent/tools/` | 给模型调用的薄接口，不重复实现业务或指标公式 |
| `services/` | 已有数据工具服务、签名事件游标和单轮取消控制；持久研究任务与报告服务未实现 |
| `analysis/cleaning/` | 已有 uint256 原值校验与 Transfer 互斥分类，不表示全部清洗方法已完成 |
| `analysis/metrics/` | 已有活动计数、分类金额、地址贡献和精确时段比较；不输出价格归因、余额或利润结论 |
| `knowledge/` | 文档检索、重排及来源引用预留，RAG 未实现 |
| `memory/` | 已有基础合同、纯规则、会话存储、独立 mem0001 迁移及运行配置；网页已接持久化，自动摘要、长期记忆和索引业务尚未完成 |
| `storage/`、`storage/repositories/` | 研究公共存储，已有 `chain_facts.py` 的 MySQL 只读事实访问；Memory 专属存储后续放在 `memory/` 内，外部 checkpoint 表与检索环境已单独初始化 |
| `tasks/` | 研究公共后台任务与 worker 预留；Memory 专属任务后续放在 `memory/` 内，不等于原有采集包的后台执行未实现 |
| `migrations/versions/` | 研究公共业务迁移预留；Memory 已在 `memory/migrations/` 独立登记迁移入口，不替代 MySQL 或账户迁移链 |

`application.py` 已提供主 Agent 流入口及分析服务公开类型，`bootstrap.py` 负责资源组装。网站聊天沿 `api → services → MainAgentApplication` 方向接入；研究包不反向依赖网站。分析工具服务另行构造，必须显式注入服务端权限解析器和分页签名密钥；真实网站数据授权源尚未注入，不在启动时迁移或建表。

Memory 按模块集中查看：业务代码放在 `python/chainscope_research/memory/`，工具在 `python/tools/memory/`，配置在 `config/memory/`，部署模板在 `deploy/memory/qdrant/`。Memory 专属的合同、服务、存储和任务不再分散到研究包的同名公共目录；共享的链上数据访问、模型能力及研究编排仍由各自公共层提供。2026-11-17 第一步建立目录与职责说明，第二步在 contracts 和 services 中实现下述合同与纯校验规则，不预建空执行函数。

环境配置为 `config/memory/services.yaml`。工具分别负责私有材料准备、数据库显式初始化、检索/Embedding 验收和 SSH 转发；部分命令会写入结构、测试数据或配置，副作用在文件头和函数注释中说明。网站当前不自动调用这些工具；检索环境与 checkpoint 表可用不等于聊天持久化或长期记忆业务已完成。2026-11-17 的仓库目录调整不移动 Linux 服务数据、Windows 私有材料或登录启动项使用的转发副本。

前端沿用 `app/layouts`、`shared/{api,format,styles,ui,validation}` 和 `features`；`auth`、`account`、`workspace` 与 `research` 实现在当前工作区，`reports`、`addresses`、`methods` 仍预留。“AI 研究”和“我的报告”是不同产品入口，报告尚不可执行。

本地入口继续放 `python/tools/`；研究配置放 `config/research_*.yaml`。主 LLM 配置由 `core/config_schema.py` 定义、`core/settings.py` 加载；工具策略由 `core/tool_settings.py` 定义与加载。类型均由 Pydantic 维护，实际参数来自 YAML；采集已有 TOML 与项目 `pyproject.toml` 保持原用途。完整消息、图状态和报告版本的存储分别设计，不能把缓存或 checkpoint 当作全部业务记录。

主 Agent 的提示词通过 `config/research_prompts.yaml` 登记逻辑名称、相对 TXT 路径、版本与变量名；`core/prompt_loader.py` 加载不可变模板，检查路径与变量并执行单次替换。`bootstrap.py` 按名称装配，不再写死正文路径。主备共享同一份本轮渲染结果，终止事件携带模板版本与已加载内容摘要。文件修改后重新装配生效，当前不支持热更新，也不缓存含用户参数的渲染结果。

四类工具描述也登记在同一 YAML，使用 `tool_<tool_id>` 名称。`agent/tools/catalog.py` 从已加载的同一目录快照取描述，不拼接 TXT 路径；参数 Schema 由 Pydantic 自动生成。`agent/tools/data_tools.py` 按启用状态和可信上下文授权创建真正的 LangChain `StructuredTool` 异步 handler，调用 `services/data_tools.py`；服务层组合 `analysis/cleaning/transfer_rules.py`、`analysis/metrics/activity.py` 与 `storage/repositories/chain_facts.py`。事实访问复用既有 MySQL 表定义，仅执行只读事务，不导入采集执行逻辑。`services/event_cursor.py` 负责签名分页。当前未向主模型自动绑定工具，也未建立分析 subagent 的工具调用循环。

`config/research_tools.yaml` 管理每实例并行度、每时段行数、时段数、证据样例、地址贡献数量和工具时限；修改后重新装配生效。同步 PyMySQL 查询和整数计算在有界线程运行，异步入口不阻塞事件循环；超时不意味着数据库已停止，槽位待线程真实结束才归还。单实例并行上限不能当作跨进程限流或上线容量证据。

`python/tools/check_research_data_tools.py --job-id bem --hours 1` 在本地数据库权限下只读验收四个实际 `StructuredTool.ainvoke`，并以独立 JOIN 和整数计算对照结果；不调用模型、RPC、迁移或重采，也不自动落盘。该本地操作者身份不能用作网站账户授权实现。

### Memory 第一步：目录骨架与职责边界

本节记录第一步的目录、职责说明和依赖方向；随后已完成的基础合同见下一节。目录存在不代表对应完整业务已实现。

```text
python/chainscope_research/memory/
  __init__.py             模块边界与当前状态
  contracts/              数据结构、输入输出和校验合同
  services/               会话、摘要、提取、裁决和管理业务
  retrieval/              向量、关键词和实体召回及结果融合
  storage/                PG、Qdrant、checkpointer 与 Outbox 访问
  tasks/                  作业执行、租约、预算、重试和恢复
  migrations/             独立迁移入口预留，当前无 env.py
    versions/             业务迁移版本预留，当前无迁移脚本
```

各 Python 包的 `__init__.py` 写明用途、依赖和导入副作用边界，仅包含中文职责说明。后续对外入口为 `memory/application.py`，资源装配为 `memory/bootstrap.py`，在产生实际能力时创建。规划调用方向为：外部调用方 → application → services → retrieval/storage；tasks 调用同一组 services；contracts 作为数据合同供各层使用。storage 不反向调用 services；后台任务不重复实现业务规则；业务模块不反向导入网站 API。

| 配套目录 | 当前状态与职责 |
| --- | --- |
| `python/tools/memory/` | 已有五个环境准备、校验和转发工具 |
| `config/memory/` | 已有 `services.yaml`，业务参数随功能增加 |
| `prompts/memory/` | 已预留；事实提取、冲突裁决和摘要的真实 TXT 随功能创建并登记 |
| `python/tests/research/memory/` | 已预留；按实际功能增加针对性验证 |
| `frontend/src/features/memory/` | 已预留；用户查看、更正和删除记忆的界面，当前无页面或路由 |
| `deploy/memory/qdrant/` | 已有 Qdrant 无秘密部署模板 |
| `deploy/memory/worker/` | 已预留；Worker 入口实现后添加部署配置 |

非 Python 预留目录使用带用途注释的 `.gitkeep` 保留，避免创建没有正文的提示词、无功能页面或不可运行的部署配置。本步未新增数据表、迁移入口、模型调用或后台进程；后续功能需要更多目录时按单一职责继续扩展。

### Memory 第二步：基础数据合同与状态规则

已实现下列纯 Python/Pydantic 能力，不连接服务或改变现有聊天协议。

| 文件（相对 memory/） | 已实现内容 |
| --- | --- |
| `contracts/common.py` | 可信用户范围、记忆代次、版本引用、保留原文的文本、安全错误码和稳定版本 Point ID |
| `contracts/conversations.py` | 新消息输入、会话、轮次、消息及可追溯摘要快照；终态原因、时间和摘要版本检查 |
| `contracts/memories.py` | 链地址绑定、实体元数据、候选事实、原文引用、ADD/UPDATE/NOOP 裁决和记忆版本 |
| `contracts/jobs.py` | 作业和 Outbox 快照、执行租约、累计次数、下次重试和失败代码；区分墓碑与物理清理 |
| `services/state_rules.py` | 轮次、记忆、作业和 Outbox 的合法转换检查；删除恢复与失败同步修复需要显式业务确认 |
| `services/decision_rules.py` | 原文来源与本轮依据检查、裁决完整覆盖、临时事实跳过、候选目标版本、用户及代次检查 |

合同沿用严格类型、拒绝额外字段和不可变结构。浏览器的新消息合同不接收身份、完整历史或生成状态；模型裁决合同不接收 user_id，UPDATE 必须绑定召回的旧版本，NOOP 不能夹带正文写入。格式合法不代表身份或事实已确认，数据库事务仍需核对实际归属、当前版本、账号状态和记忆代次。

实体合同以 `chain + address` 保存 EVM 地址绑定，再派生 `addresses` 和 `entity_keys` 供后续 Payload 使用；地址规范为小写，不将两组独立数组解释成所有可能组合。链名格式校验不验证真实链或合约身份，其他链的地址规则后续单独实现。

轮次为 `queued → running → completed/stopped/failed`，也允许排队时停止或失败；终态不回到 running，重试生成需要新轮次。作业为 `pending → running → succeeded/failed/cancelled`，可经 `retry_wait` 有界重试；Outbox 使用 `processing/synced/failed_sync/obsolete` 区分索引状态。`max_attempts` 表示包含首次在内的总领取上限，不等于额外重试次数。重复事件、并发 CAS、租约过期回收与实际退避调度后续在存储和任务层落实。

来源规则要求候选引用当前待处理轮次的最新用户消息，引用必须是允许来源中同一会话的用户原文；不接受助手消息、编造引文或仅从旧上下文重建。引用能找到不代表其语义支持候选，仍需裁决判断。删除抑制与恢复集合由可信业务流程生成，模型的 explicit_remember 标记不能直接解除删除状态。

对应离线边界验证在 `python/tests/research/memory/test_research_memory_contracts.py`。本步没有创建数据库表、迁移入口、模型客户端、Worker 或前端页面；也未将新合同直接替换到正在使用的聊天链路。

### Memory 第三步：会话数据库存储与独立迁移

`storage/database.py` 接收装配方注入的既有连接 profile，提供独立连接池及身份/TLS/权限检查；业务存储不导入网站模块。迁移入口 `migrations/env.py` 复用现有 profile 加载器，使用独立 `[memory]` 和 `research.memory_alembic_version`。`mem0001` 创建会话、轮次、消息、摘要四表，不修改账户或官方 checkpoint 的迁移链。

`storage/tables.py` 是访问列映射，不执行 create_all；数据库约束以冻结的迁移 DDL 为准。约束包括用户外键、会话/轮次复合外键、消息顺序唯一、请求幂等唯一、同一会话最多一个活动轮次、终态原因和时间、摘要覆盖消息及顺序一致性。结构变更必须新增迁移。

`storage/conversations.py` 的 `ConversationStore` 已有创建、读取、游标分页、接收新消息、开始生成、追加片段、保存终态、读取轮次和删除会话。每次变更先按可信用户锁定会话行；网络或模型等待不进入存储事务。会话 revision 在接收新消息时递增，正文追加和终态只更新活动时间，避免流式输出不断使浏览器的发送修订失效。

幂等请求先比较请求 ID、原文和基础修订，相同请求返回原轮次；不同请求竞争旧修订时只有一个被接收。新消息在一个事务内停止旧轮、保存用户原文和助手占位、分配连续 sequence。片段按服务端字符偏移追加，重复相同片段不重复写入；旧 Worker 的修订失效后拒绝写回。终态同时更新轮次和助手消息，空成功或不一致原因会回滚。

删除会话核对用户与修订，物理删除该会话并级联清理轮次、消息和摘要。长期记忆表与 checkpoint 业务接线尚未实现；后续记忆来源外键需保留记忆并清空已删原文引用，图状态清理由对应业务组织。第三步交付时网页尚未接线；第四步已接入下述持久聊天路径。

2026-11-17 已在 development/test 两库升级至 `mem0001`；运行账号可读写四张业务表、只读迁移版本表且无 DDL 权限。Memory 29 项离线合同检查和 10 项真实 PG 存储检查通过；3 项关联账户迁移检查通过。样例覆盖真实并发重放/修订冲突、跨用户访问、片段重放、迟到旧轮、终态回滚、数据库外键和级联删除，测试账号与业务行已清理。以上不代表 SSE 接线或生产容量已验收。

### Memory 第四步：运行配置与网页持久聊天接线

`config/memory/runtime.yaml` 是非秘密业务运行参数的唯一默认来源，`memory/settings.py` 使用严格 Pydantic 合同和拒绝重复键的 YAML 加载器校验。参数按 database/history/stream/recovery 分组，分别用于连接池和 SQL 等待、标题与分页、正文提交水位与线程额度、心跳与过期恢复。配置显式注入存储和网站适配，修改后重启后端生效；不在 YAML 保存用户状态、取消控制器或凭证。数据库 schema、迁移编号、状态枚举等结构性约定仍由代码和迁移维护。

`config/memory/services.yaml` 管理基础服务与索引合同；`config/web_chat.yaml` 继续管理共享聊天实例并发、SSE 和会话复核，避免两个配置文件重复控制同一参数；模型与整轮预算继续由研究 YAML 管理。

`python/webapp/api/memory/routes.py` 提供当前用户的会话创建、列表、分页详情、删除、停止与新消息流接口。网站身份来自既有 Cookie/CSRF，浏览器只提交新消息、请求编号和预期修订，不回传历史。`services/memory/chat.py` 从 PostgreSQL 构造上下文，使用主 Agent 生成，按字符或时间水位先提交正文再交付 SSE，并重排连续事件序号。完整回答和终态同步落库，重复终态请求只重放已保存结果，运行轮次不重复启动模型。

取消不提前归还仍在工作的数据库线程槽位；生成期间无正文时也刷新轮次心跳，过期记录在读取或新发送前恢复为 stopped/server_restart。显式停止先核对用户归属；断连时关闭模型流并有界保存片段及停止终态，实际进程被强制终止时只能恢复此前已成功提交的数据。多实例通过数据库修订和心跳识别旧轮，本实例额外使用控制器及时取消。

`frontend/src/features/memory/` 提供 API 投影、持久聊天 Hook 与页面外壳。`/research/new` 首次发送后替换为 `/chat/:conversationId`，替换不打断当前流；直接打开该 URL 从服务端分页恢复消息。提供历史选择、更多、刷新、新建及删除入口，停止/失败片段可见。网络结果不确定时复用原幂等键和修订；已知终态重新回答创建新轮。会话或账户切换通过代次和卸载取消隔离迟到响应，页面不把私人正文保存到浏览器持久存储。

此步未接入摘要、长期记忆 Agent 或 Qdrant 业务索引。超过上下文预算明确返回错误，尚未自动摘要。上下文使用用户消息和已完成的助手消息，停止/失败片段用于历史展示。旧 `/api/chat/turn` 继续作为兼容入口保持原协议，新网页统一使用 `/api/memory/conversations/...`。

本步针对性验证：Python 59 项通过（Memory 合同/PG 存储/持久接线及兼容聊天），前端 30 项聊天与持久路由检查通过；配置更改实际影响 SSE 批次也已验证。新持久接口使用真实 PG 和明确本地模型替身，页面路由使用 API 替身，未重新调用付费供应商。Ruff、相关 Mypy、TypeScript、ESLint 与 Vite 构建通过；本地前端与受保护 API/代理监听已验证。

## 已接通的网站聊天

```text
/research/new 或 /chat/:conversationId
  → PersistentChatRoute → ChatPage + usePersistentChat
  → /api/memory/conversations/...（Cookie、CSRF、归属与修订检查）
  → PersistentChatService → ConversationStore + MainAgentApplication
  → 正文提交 PostgreSQL → SSE 连续事件 → 页面增量显示
```

`python/webapp/app.py` 装配账户、重置密码与聊天路由。`api/routes_chat.py` 从认证会话取可信 user_id，浏览器只提交 user/assistant 文本；`services/chat.py` 懒加载并复用模型资源，为每轮隔离控制器与事件流，并在发布事件前按间隔复核会话。已有限制并发、心跳、写出超时、断连取消与资源清理；这些是当前实例的运行边界，不是跨 worker 预算或完整研究中间件。

`MainTurnRequest` 仍由同一 Pydantic 合同定义。决策阶段把带角色的历史作为序列化对话数据交给模型，正文阶段保留真实角色；`main_decision` 提示词版本为 2。主模型和备用模型共享既定单轮预算，已有正文后不拼接另一模型答案。`reply`、`clarify` 可以正常形成回复；`delegate` 只返回 `not_connected`，不会实际启动专业任务。

`ChatPage.tsx` 与 CSS 继续负责滚动、输入和跟随意图，新持久控制器来自 features/memory。原 `useChat.ts` 保留兼容协议验证；`api/chat.ts` 的 SSE 消费器由两条协议共用。新网页恢复行为见第四步；`/research/:sessionId` 仍为独立研究任务适配器，不能与新的聊天历史 URL 混用。

| 配置文件 | 已有用途 |
| --- | --- |
| `config/research_models.yaml` | 主备模型、决策/正文生成参数与单次请求设置 |
| `config/research_runtime.yaml` | 单轮时间、输入输出和尝试预算 |
| `config/research_prompts.yaml` | TXT 提示词与工具描述的名称、版本、路径和变量登记 |
| `config/research_tools.yaml` | 只读工具执行策略 |
| `config/web_chat.yaml` | 网站聊天实例/用户并发、历史条数、SSE 心跳与写出超时、会话复核间隔；由 `webapp/core/chat_settings.py` 校验加载 |
| `config/memory/runtime.yaml` | 持久聊天分页、正文提交、数据库等待、心跳和恢复水位；由 `memory/settings.py` 校验注入 |

模型实际配置可被允许的环境变量覆盖，配置修改后须重新装配/重启，不是热更新。网站登录不能代替数据集授权；三个专业 subagent、网页工具执行、报告与持久任务仍未接通。开发账户邮件只有控制台模式，生产邮件、生产账户库装配和上线验收均未完成。

2026-11-15 最近验收记录：前端 150 项通过；Python 475 项通过、221 项跳过，另有 5 个子测试通过；研究包 Mypy 42 个源文件通过，已检查范围 Ruff 通过。真实模型聊天和长消息布局分别验证，合成 SSE 不作为真实分析证据；完整记录见 [数据分析方案第 27～29 节](data-analysis-agent-design.md)。本次文档同步不重复测试。

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

`collect` 是早期小范围批次工具，其事件不属于长期任务；日常增量使用 `sync` 或采集 Agent。行情、研究分析和长期 memory 尚未接入该采集 Agent；这不表示独立研究工具和网站主 LLM 聊天仍是骨架。

## 保存与验证边界

默认采集持久化到 MySQL，不按事件生成 JSON。`raw` 用于历史证据或显式调试；`reports` 保存专项结果；Agent 进度、结束快照和报告正文存放在控制表中。它们都不代替数据库备份。

`config.ROOT` 根据源码位置找到项目根目录。打包从 `python` 加载业务包，Alembic 使用并列的迁移入口和版本目录。本地身份和数据库引用的归档路径需要保留。

已实现持久化任务、分页恢复、连续水位、独立 Swap 进度、后台执行和结束报告。2026-10-19 已完成一次 BEM 增量到明确时刻的真实闭环，证据见 README。模拟、临时 MySQL 和正式运行分别记录；这不自动证明全部代币、DEX、外部来源完整性或长期持续运行已验收。
