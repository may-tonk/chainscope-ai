# 运行与验收手册（本地 Agent 与 CLI）

更新日期：2026-10-19。命令在项目根目录的 PowerShell 执行。下文 `cs` 是 CLI 简写，可在当前终端定义：

```powershell
function cs { & .\.venv\Scripts\python.exe -B -m chainscope @args }
```

## 本地采集 Agent

```powershell
.\.venv\Scripts\python.exe -B python/tools/chat_collection.py
```

在 PyCharm 也可使用 `ChainScope Agent` 运行配置。配置或代码改变后重新运行聊天入口以加载新版本。

例如输入“把 bem 从已有进度继续采集到 2026-10-19 19:40（北京时间），并入库”。服务列表与既定预算内的明确要求直接执行，不要求复制编号。仅写时分默认当天北京时间、缺秒按 00；明确时刻不会被替换成启动时最新高度。只写“到现在”则固定准备计划时的最终确认高度。

查询或“先看计划”不启动采集；预览后可回复“确认”。输入 `你 >` 只表示仍可提问，后台结束通知不需要新输入触发，也不定时请求 LLM。

默认预算为 21600 秒（360 分钟）、1000 次采集进程 HTTP 尝试，是安全断点处检查的软上限，包含采集重试，不包含模型调用和计划准备查询。结束、暂停或失败保存事件并触发报告；四项进度达到目标才报告完成。当前来源与预算以服务列表实际配置为准。

`/exit` 退出聊天而不停止后台采集，重开补报未处理事件；“停止 bem 采集”请求安全停止。恢复原运行沿用原目标和剩余预算。额外预算及异常恢复使用 `/budget`、`/recover` 本地管理入口，不由模型自行增加额度或修改数据库。历史可通过自然语言查询，运行、事件身份和时间语义见 [数据说明](data.md)。

以下为已有 CLI 操作。日常 Agent 采集不需要逐项执行这些维护命令；修复、重做、迁移和定时注册仍是单独操作。

## 1. 首次准备

```powershell
cs init-db                      # 新环境或显式升级时执行，当前最新迁移为 0009
cs check                        # 包括与 models.py 的完整结构比对，schema_differences 应为空
cs job-create --job bem --token 0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a
```

上面的建库/建任务命令用于首次准备，已有 BEM 任务从保存进度续采，不重新创建。`job-create` 会用 eth_getCode 查找部署区块并读取 decimals。也可以按北京时间指定范围，结果显示换算出的区块和区块时间：

```powershell
cs job-create --job bem --token 0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a --from 2026-10-01T08:00 --to 2026-10-08
```

- 时间不带时区按北京时间；只接受整秒，带小数秒会报错（区块时间戳只精确到秒，同一秒可能有多个区块，这些区块总是一起算进或一起排除）。
- 范围是 [from, to)：包含开始时刻，不包含结束时刻。`--to` 不能晚于现在；省略 `--to` 表示一直跟随最新确认区块。
- 开始时间早于部署时自动改为部署区块；晚于部署时会提示之前的事件不采集、持仓不完整。
- 任务只保存区块号。再次运行 `job-create` 同名任务会被拒绝；继续采集用 `sync`，它从已保存的进度接着做。

```powershell
cs pools discover --job bem     # 扫描 PancakeSwap V2/V3 工厂事件，找出 BEM 的所有池子
cs pools list --job bem         # 报价币是 WBNB/USDT/BUSD/USDC 的池子自动启用，其余默认停用
cs pools enable --job bem --pool 0x...   # 人工确认后启用；会从池子创建区块起重做窗口
cs labels-import                # 导入 config/address_labels.toml
```

最好在第一次 `sync` 之前执行 `pools discover`，这样回填只做一遍。拆分进度后，启用新池子只重做相关 Swap 窗口，保留正式 Transfer 和它的连续进度。旧 SQD 主采任务仍沿用原来的合并窗口规则。

BEM 池子处理记录：

| 日期 | 池子 | 配对代币 | 决定与依据 |
| --- | --- | --- | --- |
| 2026-10-23 | `0x818b63ea136f9b9a0e72e20fb382e2e2b9e370d9`（PancakeSwap V3，1%） | TapeRobot | **已启用**。截至区块 129131520 有 2139 笔 BEM 进出、持有约 801 BEM。启用时重置 5 个 Swap 窗口（从 128897797 起），Swap 进度回到 128897796，正式 Transfer 不变；下一次采集补回。成交另一侧是 TapeRobot，没有美元价格 |
| 2026-10-23 | `0xe13678e66f366890f16519df71985ec24921253c`（PancakeSwap V2） | TAPEFLAP | 保持停用：截至区块 129131520 没有 BEM 进出（空池） |
| 2026-10-23 | `0xed3da4a18eaf8e82ae5ba4ab3896e5a1ab1a6805`（PancakeSwap V2） | 结晶行动 | 保持停用：截至区块 129131520 没有 BEM 进出（空池） |

三个池子已写入 `config/address_labels.toml`，均为未核实标签。配对代币是否为项目官方代币未经核实；停用的池子以后有交易时，下次 `pools discover` 后重新评估。

`pools discover` 默认从部署区块扫到最终确认高度，按段进行（`--chunk-blocks`，默认 100 万个区块，约 9 天）。每段完成就保存找到的池子，并在终端打印进度。中途失败时，报错会给出 `--from-block`，从那里继续即可。也可以先用 `--to-block` 只扫部署后的前几段。

## 2. 日常运行

2026-10-16 起，`sync` 默认采用 **Alchemy 每窗完成后正式提交转账，SQD 独立补充池子与 Swap**（不再全量重读、比对转账）。本次命令开始时固定最终确认截止区块，完成后停止；不自动追随后来新增的区块，也不自动注册定时任务。

```powershell
cs sync --job bem                              # 边采边提交正式转账，再独立补充买卖
cs sync --job bem --phase collect              # 只保存待核验转账，不进入正式统计
cs sync --job bem --phase supplement           # 提交已有完整暂存窗口，再继续补买卖
cs sync --job bem --until-block 127813580       # 继续本轮原先固定的截止范围
```

一次手动启动并在采齐后自动做库内核验、持仓链上核对和基础分析，可运行：

```powershell
.\.venv\Scripts\python.exe -B python/tools/run_bem_history.py --trace-memory
```

该入口仅运行本次固定截止范围，不注册定时任务。运行报告写在 `reports/bem-hybrid-full-*.json`；同名 `.stop` 文件用于安全停止，程序在页、窗口或发现分段的事务边界退出。发现数据冲突会停止等待人工处理。报告只有在覆盖完整、库内核验及持仓核对通过时才写 `status: completed`；测试通过或主采结束都不能替代全量完成证据。

- 迁移 0008 在 0007 基础上只新增 `block_headers`、`supplement_jobs`、`swap_windows`，保留全部旧表的数据、页游标和连续进度。
- Alchemy 每页最多 1000 条；页面与 pageKey 在同一事务提交。游标超过 8 分钟时，仅重读该窗口；旧代数据保留为证据，当前代单独计数。窗口过密则拆分，不从头重采整个历史。
- `pending_transfers` 不链接正式 `window_events`，不参与 `query`、持仓缓存和正式排行。默认完整 Alchemy 窗口通过本地字段/分页、原有回执抽查、事务读回检查即可推进 `verified_through`，标记 `local_checked`；没有声称全量双源核验。SQD 只补轻量区块元信息，不请求转账明细字段；缺失元信息可由主 RPC 补齐并缓存。
- `status` 的 `collected_through` 是连续已采集位置，`verified_through`（别名 `transfers_committed_through`）是本地检查通过的连续正式转账位置；`pending_events_saved` 包含完整/尚在分页的当前代待核验事件。`awaiting_verification_events` 只统计已采完窗口中等待核验的事件。两种进度不能混用。
- SQD 按 5 万区块发现已配置 PancakeSwap V2/V3 工厂的池子，先提交池子及相关 Swap 进度回退，再保存发现进度。中断后重复尚未记账的一段是安全的；不会跳过未提交范围。每段完成后补充已覆盖的 Swap 窗口，使用本地正式 Transfer 判断交易归属；不会重新下载整份 Transfer，也不会回退转账进度。
- Alchemy 的额度/重试耗尽型故障会切换到 SQD，仅保存待核验数据。SQD 来源数据必须等 Alchemy 恢复后才能独立核验；SQD 不能验证自己。错误参数、数据冲突不会伪装成额度耗尽。
- SQD 补充失败不撤销已提交的 Alchemy 转账。`swaps_verified_through` 单独记录买卖覆盖，查询明确标注 none/partial/complete。退出码 2 表示至少一类覆盖尚未完整，不能当作全流程采齐。隔离返回 3，其他失败返回 1。不自动付费或升级。
- 老的 `--primary sqd` 只用于尚未拆分进度的旧任务；已拆分任务拒绝切回旧窗口路径。`--no-reconcile` 不用于绕过本地入库检查。

```powershell
cs sync --job bem --max-minutes 30 --trace-memory   # 回填；可以随时 Ctrl+C，下次自动续采
cs status --job bem --max-lag-blocks 2000          # 先读当前最终确认高度；落后超过时退出码 2，读不到时退出码 4
cs query --job bem --from 2026-10-01 --to 2026-10-08
```

- `sync` 默认保留本地数量、金额合计、指纹读回，并抽查每窗最多 3 笔 Transfer 和 3 笔 Swap 回执；不做全量 SQD/Alchemy 转账比较。数据不一致的窗口被隔离：差异写入 `window_diffs`，窗口标为失败且不自动重试，水位不会越过它，相应的转账/买卖连续进度独立；命令最后以退出码 3 结束。网络或数据库的暂时失败会让命令以 1 退出，下次运行自动重试。
- 预算参数：`--max-minutes`、`--max-requests`、`--max-cu`、`--max-windows` 都是软上限。Alchemy 主采在页面之间检查，已提交页可单独恢复；SQD 补充/核验和持仓计算在分段或窗口之间检查，已开始的事务或请求可能超出预算。旧 SQD 主采路径按窗口检查，并报告 `budget_overrun_seconds`。本次手动全采没有注册定时任务或硬超时。
- 请求数按每次 HTTP 尝试计（含重试）；Alchemy 计算单元只按成功返回的调用估算。
- `status` 的 `lag_blocks` 用刚读取的最终确认高度计算，并给出读取时间 `head_observed_at_utc`；读不到时 `behind` 为 `unknown`，不会用旧高度代替。
- 连续进度以 MySQL 为准。日志由具体入口产生；手动历史入口写 JSONL。Agent 后台进程把采集事件（窗口提交、池子发现分段、暂停原因等，每个窗口一条，不含每次 529 重试）写入 `logs/agent-<run_id>.jsonl`；进程意外退出时，异常类名写入 `logs/agent-<run_id>.log`。正常进度主要保存在控制表中；暂停原因也写入运行记录的 `last_error.message`。
- `sync` 的 `quarantined` 只列本次新发现的问题，`quarantined_remaining` 是数据库中尚未处理的隔离窗口总数。旧隔离窗口在重启后仍使退出码为 3；没有可运行窗口但仍有隔离窗口时，`stopped` 为 `quarantined`，不会显示 `caught_up`。`coverage_complete` 表示连续水位是否覆盖本次的 `target_block`；预算用尽返回成功退出码只表示本轮正常停止，不代表整个任务完成。
- Portal 返回 HTTP 529（SQD 的 `rate_limit_error`/`overloaded`，公共 Portal 满载）或 429 时，按 `Retry-After` 等待后从最后收到的区块继续；连续 5 分钟没有任何成功响应才停止并报告“持续过载”。其他 5xx 和网络错误仍是连续 7 次失败后停止。失败不会当成空数据，也不会估算区块号。
- 补区块头时 SQD 失败，日志会记录 `block_headers: sqd_unavailable` 和原因。改用主节点需要每个区块一次请求；剩余请求预算不够时，本次运行暂停并报告 `BLOCK_HEADERS_UNAVAILABLE`，不消耗这些请求。已取得的区块头保留，续采时接着补。
- 窗口拆分时，日志里的 `reason` 说明原因：`event_limit` 表示读到 `at_block` 时累计 `held` 条，超过了 `event_limit`；`line_too_long` 表示单个区块的响应超过 32 MiB。
- 来源连接通过 `.env` 的 `SQD_API_KEY`、`SQD_PORTAL_URL` 等配置。套餐、流量和额度以实际账号及供应商当前规则为准，不能仅凭 HTTP 529 推断额度耗尽。程序有限重试后明确返回失败。

持仓与行情：

```powershell
cs holders --job bem --top 80             # 前 80 持有人，持仓数据截至 as_of_block
cs holders --job bem --at-block 45000000  # 历史某区块的排行（需在水位以内）
cs holders-verify --job bem --top 20      # 与链上 totalSupply、balanceOf 比对，退出码 3 表示不一致
cs market --job bem                       # 已有可选 CLI 功能，未接入采集 Agent
```

`market` 的价格、市值、成交量来自第三方，只作参考；链上价格（V2 储备或 V3 slot0）与 GeckoTerminal 相差超过代码阈值会提示。未收录时记录 listed=false；连接及限额按实际配置与接口响应处理。正常采集不要求执行行情和研究分析命令。

## 3. 定时运行

```powershell
powershell -ExecutionPolicy Bypass -File python\tools\register_sync_task.ps1
```

注册后每 10 分钟运行一次 `sync --max-minutes 9`。任务计划程序不会重叠启动；同时 MySQL `GET_LOCK` 保证同一任务只有一个进程。电脑休眠或重启后，会在下一个时间点补跑。删除定时任务：`Unregister-ScheduledTask -TaskName "ChainScope BEM sync" -Confirm:$false`。

## 4. 复查与重做

```powershell
cs recheck --job bem --sample 20      # 重新拉取已完成窗口，逐字段比对 Transfer 和 Swap；有变化就隔离并回退水位，退出码 3
cs redo --job bem                     # 把解析版本低于当前版本的窗口重置为待采集
cs redo --job bem --from-block X --to-block Y   # 指定范围：也放行被隔离的窗口
cs verify-job --job bem               # 只查 MySQL 内部：指纹、Swap 条数、同一区块的哈希和时间是否唯一
```

- 分工：`verify-job` 不访问外部数据源，检查库内自洽；`recheck` 重新拉取，检查外部数据是否与库内一致（区块号、区块哈希、时间、地址、金额，以及 Swap 的方向、数量、交易人、归属）。复查时如果某窗口的池子集合已经变了，它的 Swap 不比对，输出会写明。
- 最终确认区块的数据发生变化本不应出现。出现时窗口被隔离，原有记录不会被覆盖（`redo` 后重采仍会因内容冲突再次隔离）。请把 `window_diffs` 里该窗口的记录发给我，确认是哪个来源出错后再决定如何修正。
- 解析版本：Transfer 和 Swap 来自同一次请求，共用一个解析版本号；每个窗口记录实际运行的版本，`redo` 不带范围时只重置低于当前版本的窗口。

## 5. 备份与恢复演练

备份放在项目外的 `E:\个人网站\chainscope-backups`。截至 2026-10-23 只保留一份（区块 128301272、迁移 0008），可恢复范围和之后需要补采的部分见 [数据说明](data.md) 的“数据库备份现状”。新建或删除备份后同步更新那一节。

```powershell
mysqldump --single-transaction --routines --databases chainscope > backup\chainscope-YYYYMMDD.sql
mysql -e "CREATE DATABASE chainscope_restore"
# 把备份中的 `chainscope` 替换为 chainscope_restore 后导入，或用 Navicat 恢复到新库
$env:MYSQL_DATABASE = "chainscope_restore"; cs verify-job --job bem; cs status --job bem
Remove-Item Env:MYSQL_DATABASE
```

记录备份文件大小、恢复用时，以及 verify-job 结果为 `ok: true`。

## 6. 故障注入（强退恢复演练）

```powershell
foreach ($point in "after_fetch", "before_done", "before_commit", "after_commit") {
    $env:CHAINSCOPE_FAULT = $point
    cs sync --job bem --max-windows 1          # 进程在指定位置直接退出（退出码 75）
    Remove-Item Env:CHAINSCOPE_FAULT
    cs sync --job bem --max-windows 1          # 应自动恢复：recovered_abandoned 为 0 或 1
    cs verify-job --job bem                    # 必须 ok: true
}
```

## 7. 规模验收记录表

| # | 项目 | 命令或做法 | 记录 |
| --- | --- | --- | --- |
| 22 | 内存峰值 | `sync --trace-memory` 的 `memory_peak_mib`；离线基准 `python python\tools\bench_limits.py` | |
| 51 | 存储 | `cs report` 的 tables | |
| 52、53 | 查询耗时与索引 | `cs report` 的 queries；在 sync 运行时再测一次 | |
| 54 | 迁移耗时 | `Measure-Command { cs init-db }`（升级前先备份） | |
| 55 | 备份恢复 | 第 5 节 | |
| 56、65 | 持续运行 | 第 3 节运行 48 小时，期间 `status` 的 behind 都为 false | |
| 60、62 | 代表性窗口 | 选最密集的一天、空范围、铸币/销毁、一笔多日志的范围，用 `redo` 重做并对账 | |
| 61 | 全量回填 | 从部署区块 sync 到 caught_up，记录 elapsed_seconds 和 usage | |
| 63 | 强退恢复 | 第 6 节 | |
| 64 | 长时间停机 | 停 1 天后再运行 sync，确认自动补齐 | |

## 8. 网站账户库（PostgreSQL，开发阶段）

账户库在 VMware Linux 虚拟机 `192.168.229.10:5432`，只接受来自 Windows VMware 网卡的 TLS 连接（`verify-full`）。开发前启动虚拟机，并确认 PostgreSQL 在运行。

1. 按 `.env.example` 在 `.env` 填写 `WEBAPP_DB_*` 和 `WEBAPP_TEST_DB_*`。运行网站后端前，还要用 `python python/tools/generate_webapp_keys.py` 生成 4 把 `AUTH_*_KEY` 并粘贴进 `.env`。自动化测试使用临时密钥，不需要这一步。`WEBAPP_DB_SSLROOTCERT` 填 CA 证书路径 `D:\Linux2\certificates\chainscope-postgresql-ca.crt`。密码只写在 `.env`。
2. 运行只读连接检查，全部 `"ok": true` 才继续：

   ```powershell
   .\.venv\Scripts\python.exe python/tools/check_webapp_db.py
   ```

   它检查四个账号能否用 TLS 连上各自的库、运行账号不能建表、迁移账号不是超级管理员，并确认未加密连接被拒绝、开发运行账号连不上测试库。
3. 先在测试库验证，再升级开发库：

   ```powershell
   $env:CHAINSCOPE_TEST_POSTGRES='1'
   .\.venv\Scripts\python.exe -m pytest -q python/tests/webapp/test_web_migrations.py
   Remove-Item Env:/CHAINSCOPE_TEST_POSTGRES
   .\.venv\Scripts\python.exe -m alembic -n webapp upgrade head
   ```

   测试结束后，测试库会回退为空。测试库里如果有不属于账户迁移的表，测试会停下且不改动。开发库升级后，在 Navicat 中应看到 5 张表和 `alembic_version`（版本 `0001`）。

启动网站后端（开发环境，只监听本机）：

```powershell
.\.venv\Scripts\python.exe python/tools/run_webapp.py
```

需要 `.env` 里已有 4 把 `AUTH_*_KEY`。启动后打开 `http://127.0.0.1:8000/docs`，可以查看并试用接口。开发模式下，注册验证码打印在这个窗口里。按 Ctrl+C 停止。

服务器证书 2027-11-26 到期，到期前在虚拟机换证书。虚拟机 IP 或 Windows VMware 网卡 IP 变化时，先同步修改数据库端的访问控制和证书。

