# ChainScope CLI

基于 Python、Alchemy 和 MySQL 的 BSC ERC-20 转账采集工具。输入代币合约地址与固定区块范围，程序完成分页采集、精确金额解析、交易回执核验、事务入库和数据库读回核对，为后续地址分析、查询 API 和网站提供事件明细。

当前发布为第一阶段数据采集版本。已验证小范围端到端行为；尚未完成百万级吞吐、连续覆盖、断点恢复和生产部署验收。

## 已实现功能

- BSC 主网识别、合约检查、固定区块范围和分页采集。
- 从 `rawContract.value` 解析整数，完整保存 uint256 金额，不使用浮点数参与准确计算。
- 回执对照交易、区块、日志索引、Transfer 签名、地址和金额；默认检查前 10 个不同交易中的已采集事件，并报告核验范围。
- 连接/读取超时、最多 3 次请求尝试、HTTP 429/指定 5xx 重试；有效空结果与失败分别处理。
- MySQL 事务、事件唯一约束、重复数据复用、内容冲突回滚、批次状态追踪。
- Alembic 版本化迁移：`0001` 初始化表，`0002` 支持没有归档的批次。
- 默认 `collect` 只持久化 MySQL，不生成 JSON 文件；显式调试归档有单批和总容量限制。

## 技术栈

Python 3.12、httpx、SQLAlchemy Core、PyMySQL、Alembic、pytest、Ruff、uv。Web3.py 与分析/API 工具已作为依赖预备，当前采集主流程采用同步 httpx，并未实现异步采集或 AI 功能。

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

## 可选调试归档

需要排查接口响应时，在 `collect` 上添加 `--save-raw`；默认关闭。调试输出保存成功请求参数/响应和 `manifest.json`，不生成 `events.json`。

- 单批默认 10 MiB，`--archive-max-mib` 可调整，最大不超过 100 MiB。
- 整个 `raw/` 的文件内容总量最多 100 MiB，已有历史文件也计入。
- 共享写锁串行化容量检查和写入，使用临时文件原子发布；超限明确失败。
- 不自动删除旧文件；没有实现自动过期清理、压缩或归档回放。
- 归档不是数据库备份，也不是事件正确或完整的证明。

## 测试与验证证据

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

设计说明见 [模块职责](docs/architecture.md)、[版本化迁移](docs/database-migrations.md)、[数据用途](docs/data.md)、[业务工程路线](docs/engineering-roadmap.md)。Navicat 核对查询见 [acceptance.sql](sql/acceptance.sql)。

## 当前边界与后续工作

- 事件暂时在整个有限批次内累积，数据库逐条查重/写入；分批吞吐和内存需要进一步优化与实测。
- 未实现持久化进度、自动续跑、连续区块覆盖水位和链重组修复。
- 回执对照验证返回事件，不能证明供应商没有遗漏；金额总和是读回计算，不是独立完整性对账。
- 结束区块要求至少 12 个后续区块，只是当前链头避让规则，不构成最终性证明。
- 当前提供单链、单合约、固定范围 CLI；业务 SQL、API、调度、监控和部署继续在同一项目扩展。
