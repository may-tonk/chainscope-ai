# ChainScope 采集 CLI 与本地 Agent

基于 Python、Alchemy、SQD 和 MySQL 的 BSC 数据采集项目。支持 CLI 和 DeepSeek 本地对话入口，保存标准 Transfer、配置范围内的池子与 Swap，并维护连续进度和持仓缓存。输入明确采集范围后，程序完成分页、检查、事务入库和结果报告，为后续研究与查询提供数据。

当前包含数据采集 CLI 与本地对话采集 Agent。实际采集规模、截止位置和数据核对结果以对应运行记录为准；本地运行不代表已经完成网站部署或所有来源、事件类型的覆盖验收。

## 已实现功能

- BSC 主网识别、合约检查、固定区块范围和分页采集。
- 从 `rawContract.value` 解析整数，完整保存 uint256 金额，不使用浮点数参与准确计算。
- 回执对照交易、区块、日志索引、Transfer 签名、地址和金额；默认检查前 10 个不同交易中的已采集事件，并报告核验范围。
- 连接/读取超时、最多 3 次请求尝试、HTTP 429/指定 5xx 重试；有效空结果与失败分别处理。
- MySQL 事务、事件唯一约束、重复数据复用、内容冲突回滚、批次状态追踪。
- Alembic 版本化迁移 `0001`～`0009`：包含批次、长期任务、分页暂存、独立补充和 Agent 控制表。
- 默认 `collect` 只持久化 MySQL，不生成 JSON 文件；显式调试归档有单批和总容量限制。

## 技术栈

Python 3.12、httpx、SQLAlchemy Core、PyMySQL、Alembic、pytest、Ruff、uv。采集主流程采用同步 httpx；对话入口使用 OpenAI Agents SDK 和 DeepSeek。Web3.py 与分析/API 工具已作为依赖预备，研究分析与网站接口尚未接入 Agent。

## 本地采集 Agent

采集 Agent 使用 OpenAI Agents SDK 和 DeepSeek Chat Completions 接口，复用已有采集流程。模型选择工具；本地程序校验参数、授权、任务互斥和固定截止位置。行情、研究分析和长期 memory 尚未接入。

```powershell
uv sync --all-extras --locked
# .env 设置 DEEPSEEK_API_KEY；DEEPSEEK_MODEL 默认 deepseek-flash
# 单独验证真实模型与只读进度工具，不启动采集
.\.venv\Scripts\python.exe -B -m chainscope.agent_runtime --check
# 本地对话入口（也可在 PyCharm 运行 python/tools/chat_collection.py）
.\.venv\Scripts\python.exe -B -m chainscope.agent_runtime
```

完整对话入口要求迁移 `0009` 的四张 Agent 控制表。部署前先备份并审阅 `python/migrations/0009_agent_control.py`，再通过已有 `init-db` 执行迁移；不会由聊天自动升级数据库。旧 `0008` 仍可使用原采集命令和只读进度检查。

`config/supported_tokens.toml` 是本地维护的 BSC 代币服务列表，以链和合约地址确定身份；初始登记 BEM，添加其他代币无需复制代码。列表外返回 `TOKEN_NOT_SUPPORTED`；停用代币不能新采集或续采，但已有运行仍可查看、停止。创建新代币任务在具体计划获得授权后执行。

五个采集工具为 `get_collection_status`、`prepare_collection`、`start_collection`、`get_collection_run`、`stop_collection`，另外提供只读的 `list_collection_runs` 和 `get_collection_event`。Schema 与本地参数校验来自同一份 Pydantic 类型定义。DeepSeek 标准接口不冒充 beta strict 模式；所有工具参数和输出仍通过本地合同校验。OpenAI 远程 tracing 关闭，凭证不进入模型上下文。

可输入“查看 bem 的本地采集进度”“把 bem 从已有进度继续采集到现在，并入库”。在服务列表和既定预算内，明确的采集指令直接启动；只展示简短中文范围、截止时间和预算，不要求复制计划编号或阅读完整 JSON。授权由本地程序绑定用户原话和实际计划，模型不能自行签发、扩大范围或新增预算。查询和“先看计划”不会启动采集；查看计划后可输入“确认”执行。停止也需要用户明确指令，例如“停止 bem 采集”。启动请求被接受不表示采集已经完成。

明确指定时间时按该时刻定位最后一个不晚于它的最终确认区块，不改成准备计划时的最新高度。例如“采集 bem 到2026-10-19 19:40，并入库”使用北京时间 19:40:00；仅写时分默认当天北京时间，写了日期或时区则依其处理。时间不明确、尚未到达，或对应范围尚未全部最终确认时，说明原因而不擅自替换截止位置。只写“到现在”则固定本次准备时的最终确认高度。恢复暂停的原运行沿用原目标和剩余预算。

采集使用独立后台进程；关闭聊天不会停止它。`/exit` 退出，`/new` 仅清空当前对话。进度以持久化记录为准；请求停止后等待安全断点，已提交数据保留。续采使用原运行编号，固定终点和已消耗预算都不重置。

执行完成、暂停、停止或失败后，最终状态和结束事件快照在同一事务中保存到已有 Agent 表，本地进程通信随即唤醒对话入口。程序监测工作进程退出，不按固定间隔请求模型或扫描链上数据。报告轮只允许模型读取绑定的 `event_id`；模型自然组织中文说明，程序核对编号、状态并附上快照中的进度、时间及用量。`run_id` 标识整个逻辑运行，`attempt_id` 区分每段执行，`event_id` 定位一次结束结果；恢复采集不会改写旧结果。预算和新增计数为逻辑运行累计值，未统计的 Swap 新增数不填零。

`agent_events.payload` 保存不可变结果和独立的报告处理信息，无需新增表或升级 `0009`。报告正文先保存再展示；重复通知不重复生成，重开对话会补报未处理事件。模型生成最多尝试两轮，失败保留记录，在同一聊天中不无限重试。生成成功但尚未展示的正文直接复用。展示后确认落库前若程序崩溃，重开可能补发一次；仅查看历史不触发重新采集。普通终端用 prompt-toolkit 保留正在编辑的输入；PyCharm 普通 Run 控制台采用单个后台输入线程，通知可换行显示且不会消费授权输入。

可输入“列出 bem 的历史采集运行”“查看事件编号 123 的结束报告”。历史列表按运行创建时间查询（`created_from` 包含、`created_to` 不包含，ISO 8601 必须带时区，如 `2026-10-19T00:00:00+08:00`），支持最多 50 条及游标分页；时间不是编号，也不是链上数据截止时间。数据库时间字段按 UTC 保存，展示采用北京时间。历史列表返回最新结束事件编号，已知旧事件编号也可精确读取旧快照和报告。

服务列表内的预算默认每个逻辑运行 1800 秒、1000 次采集进程 HTTP 尝试，均为软上限，包含采集进程内重试和启动校验；不包含模型请求及计划准备查询。额度耗尽只暂停，不自动增加额度。`/budget <运行编号> <新增秒数> <新增请求数>` 在本地核对授权后增加额度。`/recover <运行编号>` 仅核对并恢复已确认结束或未被领取的执行，不自动续采；中断用量不确定时保守计入原额度，必须明确授权新增预算。

`config/agent_local.toml` 保存当前本地身份并被 Git 忽略，跨聊天访问已有运行需要保留它。适用范围是单机本地操作者，不提供公网多用户认证服务。数据冲突继续隔离并报告，Agent 没有删除正式记录、修改采集规则或强制放行权限。

## 快速开始

要求 Python 3.12、uv、MySQL >= 8.0.16，以及可调用 BSC Transfers API 的 Alchemy 连接。以下使用 PowerShell，从仓库根目录执行。

```powershell
uv sync --all-extras --locked

# 首次创建本地配置，已有配置不覆盖
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

在本地 `.env` 填写 `BSC_RPC_URL`、`MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`。配置不会提交到 Git；环境变量优先于文件。数据库账户需要目标数据库的初始化和读写权限。Navicat 可连接同一 MySQL 服务检查结果。

```powershell
# 创建指定数据库并升级到最新结构，重复执行不会重复建表
uv run --all-extras --locked chainscope init-db

# 检查 BSC RPC、MySQL 连接及迁移状态
uv run --all-extras --locked chainscope check

# 真实历史示例：4Stock 合约，20 个区块，两端包含
uv run --all-extras --locked chainscope collect --token 0xd270d4e1ec6e6e0d28c0ecb8be966ec75997ffff --from-block 120607788 --to-block 120607807

# 用 collect 输出的实际批次 ID 读回核对
uv run --all-extras --locked chainscope verify --batch-id <batch-id>
```

`collect` 输出终端摘要，数据存入 MySQL。重复执行示例会新增执行批次和来源关联，内容一致的事件不会重复插入；未开启归档时 `raw_directory` 为 SQL NULL。

## 数据流与表设计

```text
CLI / 配置 → 固定范围分页采集 → 精确解析 → 回执核验
          → MySQL 事务提交 → 数据库读回核对 → 终端摘要
```

| 表 | 用途 |
| --- | --- |
| `transfer_events` | 规范化转账明细；唯一键为 `(chain_id, tx_hash, log_index)` |
| `collection_batches` | 执行范围、状态、数量、结束区块哈希、错误类型及可选归档位置 |
| `batch_events` | 事件与执行批次的来源关联，保留重复采集记录 |
| `scan_jobs` | 长期采集任务：一个代币一个任务，记录起点、窗口大小、解析版本和连续水位 `verified_through` |
| `coverage_windows` | 任务的覆盖窗口（两端包含）：状态、尝试次数、事件数、金额合计和指纹；窗口就是断点 |
| `window_events` | 事件与窗口的关联；同一窗口重采不会新增行 |
| `window_diffs` | 与 Alchemy 对账或复查发现的差异 |
| `dex_pools` | 任务跟踪的交易池（PancakeSwap V2/V3 工厂发现或手工添加）及是否启用 |
| `swap_events` | 跟踪池子的买卖：方向、代币和报价币数量、交易发起人、归属可信度 |
| `address_balances` | 由 Transfer 累加的持仓，最高到 `scan_jobs.balances_through` |
| `address_info` / `address_labels` | 是否合约（eth_getCode）、地址标签（来源和是否人工核实） |
| `market_snapshots` / `pool_snapshots` / `pool_ohlcv` | CoinGecko 行情、池子价格与储备核对、4 小时 K 线（第三方，仅供参考） |

`amount_raw` 使用规范十进制字符串 `VARCHAR(78)` 保存完整 uint256，数据库 CHECK 限制格式与上限。MySQL DECIMAL 的精度不足以覆盖完整 uint256；准确汇总由 Python 整数完成，不能直接通过 `SUM(amount_raw)` 隐式转换。

相同事件内容一致就复用，内容冲突则回滚当前批次的事件写入。批次成功状态与事件、关联在同一事务提交。数据库提交后的核对或可选文件更新失败，会明确提示该批次已经提交，不把成功记录改成失败。

## 命令

| 命令 | 行为 |
| --- | --- |
| `collect` | 采集、核验、入库和读回核对，默认不产生文件 |
| `verify` | 读取已有批次，核对状态/数量并计算原始金额汇总 |
| `check` | 检查 BSC 主网、数据库连接和结构版本 |
| `init-db` | 初始化指定数据库并执行迁移 |
| `fetch` | 采集、核验并保存有限额的调试响应，不入库 |
| `inspect` | 检查历史 CSV 的有限样本，不重新采集 |
| `job-create` | 创建长期采集任务；起点可用 `--start-block` 或北京时间 `--from`，`--to` 固定终点（不含）；总会记录部署区块 |
| `sync` | Alchemy 按窗口采集并正式入库；SQD 独立补充池子与买卖；固定本次截止位置，可中断续采 |
| `status` | 读取当前最终确认高度后计算落后区块；`--max-lag-blocks` 超出时退出码 2，读不到高度时退出码 4 |
| `recheck` | 随机重新拉取已完成窗口，逐字段比对 Transfer（含区块号、哈希、时间）和 Swap；有变化的窗口隔离并回退水位 |
| `redo` | 把旧解析版本或指定区块范围的窗口重置为待采集；指定范围也会放行被隔离的窗口 |
| `query` | 按北京时间 [from, to) 统计转账和买卖；只统计连续水位以内的区块，并分段说明部署前、未采集、已核实、未核实 |
| `report` | 各表存储占用与三类查询耗时、所用索引 |
| `verify-job` | 从 MySQL 重新计算每个完成窗口并核对指纹 |
| `pools discover/list/add/enable/disable` | 发现或管理交易池；启用后从池子创建区块起重做窗口以补齐买卖 |
| `holders` | 持仓排行（默认前 80），附标签、是否合约、占比；`--at-block` 查历史 |
| `holders-verify` | 在水位区块用链上 totalSupply 和 balanceOf 核对持仓，并记录是否合约 |
| `labels-import` | 从 `config/address_labels.toml` 导入地址标签 |
| `market` | 保存 CoinGecko 价格/市值/24h 成交量、GeckoTerminal 池子行情和 4h K 线，并与链上价格比对 |

### 长期采集 BEM

```powershell
uv run --all-extras --locked chainscope init-db        # 升级到当前最新版本 0009（保留旧表和进度）
uv run --all-extras --locked chainscope job-create --job bem --token 0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a
uv run --all-extras --locked chainscope sync --job bem --max-minutes 30
uv run --all-extras --locked chainscope status --job bem
```

- `sync` 先取得 MySQL `GET_LOCK`，同一任务只允许一个进程；进程意外退出后锁自动释放，遗留的 running 窗口在下次运行时标为 abandoned 并重做。
- 窗口按区块升序处理，失败窗口优先重试；水位只沿连续完成的窗口推进，不会越过空洞。没有事件的窗口也记录为完成。
- 单窗口事件超过 `--event-limit`（默认 5 万）时自动对半拆分；每个窗口一个事务，批量查重和写入，死锁/断线有限重试，提交确认丢失时以数据库窗口状态为准。
- 入库后从 MySQL 流式读回，按数量、金额合计和指纹核对。
- 默认采用 Alchemy 主采转账，每个完整窗口通过字段、分页、原有回执抽查和事务读回检查后立即正式入库，记录 `reconcile=local_checked`，不再从 SQD 下载整份转账做双源比较。未完成页与异常记录仍隔离。`verified_through` / `transfers_committed_through` 表示本地检查通过的连续正式范围，不代表全量双源核验。
- 每次手动命令开始时固定最终确认截止位置，采到该位置后停止。Alchemy 故障时可改由 SQD 暂存，但必须等另一来源恢复、核验通过才能进入正式统计；不使用 `--no-reconcile` 绕过此要求。
- `--phase collect` 保留为仅暂存模式；默认 `--phase all` 边采集边提交正式转账。`--phase supplement` 可提交已保存的完整窗口，再续做池子/买卖补充。`swaps_verified_through` 单独记录买卖覆盖，新池子只回退买卖进度，不回退正式转账。退出码 2 表示至少一类覆盖未完成。
- Alchemy 正常主采不做全量双源转账比较。SQD 仅补充轻量区块头和池子/Swap；区块头缓存可复用，缺失或不可用时可从主 RPC 补齐。原有每窗最多 3 笔 Transfer 和 3 笔 Swap 的回执抽查保留，不能据此证明供应商没有遗漏。
- Swap 只读取已经正式入库的 Transfer 做交易归属，不再次请求外部转账。`query` 对转账与买卖分别报告覆盖范围；未补齐买卖范围不能当成没有交易。写入/读回、区块元信息、回执或已存数据发生冲突时隔离相应范围，不推进对应进度。
- 持仓只累加到连续水位，只计入窗口关联的 Transfer。水位回退不超过 20 万块时在同一事务里减去回退部分，更长时清空后分块重建；每块一个事务并锁住任务行，并发刷新也只会加一次。`holders-verify` 不一致通常说明是反射/分红型代币或起点晚于部署。
- 金额按原始整数字符串保存（最多 78 位，覆盖 uint256）。`query` 的合计用 Python 整数逐行相加，任意长度都精确；持仓缓存用 MySQL DECIMAL(65,0)，可能超过时拒绝计算并说明原因，不会截断。
- 内存上限：一个窗口最多约 5 万条（Transfer+Swap，`--event-limit`），超过自动拆分；单个区块不能拆，上限也是 5 万条（`--block-hard-limit`），超过就隔离。实测每条约 3 KB，5 万条约 140 MiB。响应大小在读取过程中检查：RPC 16 MiB、Portal 每个区块 32 MiB、其他 Portal 请求 1 MiB、行情接口 8 MiB。
- 运行、定时任务、备份恢复和故障注入演练见 [运行与验收手册](docs/operations.md)；方案见 [BEM 采集解决方案](docs/BEM采集解决方案-2026-10-15.md)。

## 可选调试归档

需要排查接口响应时，在 `collect` 上添加 `--save-raw`；默认关闭。调试输出保存成功请求参数/响应和 `manifest.json`，不生成 `events.json`。

- 单批默认 10 MiB，`--archive-max-mib` 可调整，最大不超过 100 MiB。
- 整个 `raw/` 的文件内容总量最多 100 MiB，已有历史文件也计入。
- 共享写锁串行化容量检查和写入，使用临时文件原子发布；超限明确失败。
- 不自动删除旧文件；没有实现自动过期清理、压缩或归档回放。
- 归档不是数据库备份，也不是事件正确或完整的证明。

## 测试与验证证据

2026-10-19 最近完整回归：**358 项通过、2 项可选模型测试跳过、5 项 subtests 通过**。真实 DeepSeek 的单条指令启动和自动结束报告另行通过，使用临时库与模拟采集来源。

同日正式 BEM 增量运行于北京时间 20:28:31 开始、20:46:51 完成，转账、Swap、池子发现和持仓缓存均达到明确指定的 **19:40:00、区块 128547796**。本运行新增转账 85,509 条，使用约 1099.7 秒、315 次采集 HTTP 尝试。报告于 20:46:54 生成并记为已展示；该标记表示程序完成输出和记账，不代表操作者已经阅读。结束时任务累计转账 6,846,103 条、Swap 788,188 条；本运行新增 Swap 未单独统计。这是固定时刻的历史证据，不代表之后的数据也已采集。

以下为可复现命令和带日期的较早验证记录。模拟、临时数据库和正式数据证据分别说明，不能由其中一种推导全部来源、代币或规模已验收。

```powershell
# 默认测试使用模拟 RPC，不需要真实 RPC 或数据库
uv run --all-extras --locked python -m pytest -q
uv run --all-extras --locked python -m ruff check python

# 使用随机独立测试数据库，结束后仅删除测试创建的数据库
$env:CHAINSCOPE_TEST_MYSQL='1'
uv run --all-extras --locked python -m pytest -q
Remove-Item Env:/CHAINSCOPE_TEST_MYSQL

# 观察网络、超时、限流、RPC 错误和空结果的可控模拟
uv run --all-extras --locked python python/tools/demo_rpc_failures.py --scenario all
```

2026-10-15 评审修正后：**153 项测试在 MySQL 8.4 和 8.0.43 上都通过**（另有 5 项 subtests），全部是模拟数据源，没有访问真实 SQD、Alchemy 或行情接口。各评审项的状态见 [评审修正状态](docs/评审修正状态-2026-10-15.md)。

2026-10-16 主采调整：本机 MySQL 8.0.43 的完整回归 **179 项、5 项 subtests 通过**，随后增加的正式提交中断恢复、Swap 双来源流程两项专项测试也通过。备份恢复后升级 0007 的演练确认旧 15 张表保持一致。另在独立临时库做过 BEM 20 区块真实联调：1 条转账先隔离为待核验、正式统计为 0，核验后才提交为 1。该证据只证明小范围联通与隔离流程，全历史采齐和规模性能以实际运行报告为准。

2026-10-16 分窗正式入库调整：完整回归 **190 项测试、5 项 subtests 通过**；迁移 0008 的备份恢复演练和正式升级均确认原有 18 张表内容校验一致。独立临时库 BEM 20 区块真实联调确认转账可先正式提交，之后补充买卖不会再次拉取 Alchemy 转账。全量历史尚在采集中。

2026-10-13 本地验收：包含真实 MySQL 的 **36 项测试通过，另有 5 项 subtests 通过**；Ruff 检查通过。覆盖精确金额、分页重复、错误响应、回执冲突、数据库约束、事务回滚、默认零归档、归档容量及并发写入、提交后归档失败等场景。

在线验证使用合约 `0xd270d4e1ec6e6e0d28c0ecb8be966ec75997ffff`、区间 `120607788–120607807`：取得 8 条事件、对应 4 笔交易，回执全部对照通过。默认模式重复采集复用 8 条事件，新增归档文件为 0。该证据仅覆盖此小范围，不能推导千万级性能或完整历史覆盖。

`samples/sample_transfers.csv` 是旧采集项目的 20 条固定历史样本，供 `inspect` 使用；不参与在线采集，不能代替容量测试。运行数据、本地凭证、机器诊断和个人学习记录不上传。

## 项目结构

人工维护目录最多嵌套一层，按文件类型组织：

```text
python/
  chainscope/       业务 Python 代码
  tests/            测试 Python 代码
  tools/            工具 Python 脚本
  migration_env/    Alembic 执行入口
  migrations/       迁移版本
sql/                SQL 查询与历史初始迁移导出
samples/            固定 CSV 样本
reports/            样本来源；运行报告留在本地
raw/                可选调试归档，运行时创建，不提交
  <batch-id>/       JSON 响应和摘要
.run/               PyCharm 模块运行配置
```

当前说明见 [模块职责](docs/architecture.md)、[版本化迁移](docs/database-migrations.md)、[数据与文件保存](docs/data.md)、[运行手册](docs/operations.md)、[测试方法](docs/mysql-tests.md) 和 [Navicat 查询](docs/sql-queries.md)。[业务工程路线](docs/engineering-roadmap.md) 区分当前基线与后续目标；带日期的旧方案和学习记录保留其历史身份。

## 当前边界与后续工作

- `collect` 与 `sync` 的边界：`collect` 是早期的固定区间工具（用 Alchemy 转账接口取数据，整批在内存中处理），写入的事件不属于任何任务，`query`、`holders` 和 `verify-job` 都不会计入。BEM 的长期数据只通过 `sync` 采集；`collect` 只用于小范围排查。
- `sync` 已实现持久化进度、自动续跑、连续水位、本地检查、独立买卖覆盖及可显式执行的跨源复查；已最终确认区块的冲突只记录并停止，不自动修复；规模验收需在真实数据上按运行手册完成。
- 回执对照验证返回事件，不能证明供应商没有遗漏；金额总和是读回计算，不是独立完整性对账。
- `collect` 的结束区块要求至少 12 个后续区块，只是链头避让规则，不构成最终性证明；`sync` 用的是最终确认高度。
- 当前提供 BSC CLI 和服务列表内代币的本地采集 Agent，一次操作一个明确代币和固定终点。研究分析、长期 memory、网站接口和公网多用户部署尚未接入 Agent。
