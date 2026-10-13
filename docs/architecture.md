# 项目目录与模块职责

全部人工维护文件按类型放置，目录最多嵌套一层。后续功能沿用这一结构。

## 目录

```text
chainscope-ai/
  python/                       # 只放 Python
    chainscope/                 # 业务模块，直接放 .py
    tests/                      # 单元测试和 MySQL 测试，直接放 .py
    tools/                      # 环境检查、异常演示，直接放 .py
    migration_env/              # Alembic 运行入口 env.py
    migrations/                 # 迁移版本 0001、0002
  sql/                          # acceptance.sql、migration-0001.sql
  docs/                         # 所有已有 Markdown，平铺
  samples/                      # CSV 历史样本
  reports/                      # JSON 验收报告、样本来源元数据
  raw/                          # 可选调试 JSON 和保留的历史证据
    <batch-id>/                 # 新调试批次只有 RPC 响应和 manifest.json
  logs/                         # 日志输出
  .run/                         # PyCharm XML 运行配置
  .venv/                        # 自动管理的依赖环境
  build/                        # 旧缓存、工具元数据和构建输出
  pyproject.toml                # 依赖、打包和测试配置
  uv.lock                       # 固定依赖版本
  requirements-dev.txt          # 固定版本的 pip 导出
  alembic.ini                   # 迁移路径配置
  .env / .env.example            # 私密配置与模板
  .gitignore
  AGENTS.md
  README.md
```

根目录保留打包、环境与协作工具要求的入口文件。`.venv`、缓存、egg-info 和 build 是自动生成内容，在 PyCharm 中排除；一层限制针对人工维护内容，不修改依赖包内部结构。整理时旧目录中的缓存、占位符和 IDE 元数据保留在被排除的 build，运行不依赖这些内容。

## Python 文件职责

| 文件（均位于 python/chainscope） | 职责 |
| --- | --- |
| __init__.py | 包标识 |
| __main__.py | CLI 参数、命令分派、错误和退出码 |
| config.py | 设置、私密配置、项目根路径 |
| errors.py | 可安全显示的业务错误 |
| pipeline.py | 批次生命周期、采集与入库编排 |
| rpc_client.py | JSON-RPC 请求、超时、重试、成功响应归档 |
| archive.py | 显式调试输出的单批/总容量检查、共享锁和原子文件发布 |
| collector.py | 固定区间、分页、调用解析与核验 |
| parser.py | 精确字段和金额转换 |
| validator.py | 交易回执核验 |
| models.py | 当前 SQLAlchemy 表定义 |
| database.py | MySQL 连接、迁移、事务、去重、读回核对 |
| records.py | 历史 CSV 样本检查 |

CLI/config → pipeline → collector → rpc_client/parser/validator → database → 数据库读回核对。

collect 默认先记录数据库批次，在内存解析和核验，再在同一 MySQL 事务提交事件、批次关联和成功状态，最后读回核对；不创建 raw 批次目录或 JSON。显式 --save-raw 和 fetch 才保存调试 RPC 响应与摘要，受单批默认 10 MiB、raw 总内容 100 MiB 限制，不生成 events.json。保留旧文件，未实现自动到期清理和归档回放。永久进度、自动恢复和大规模性能仍待实现。

## 路径与运行

pyproject.toml 从 python 查找并安装 chainscope；pytest 从 python/tests 发现测试。config.ROOT 仍定位项目根目录，.env 和解释器位置不变。PyCharm 父工作区源码根已改为 chainscope-ai/python。

Alembic 的 script_location 指向 python/migration_env，version_locations 指向并列的 python/migrations，避免 migration/versions 再增加一层。冻结的 0001 不改写；0002 允许 raw_directory 为 NULL，表示批次没有归档。

```powershell
# 从仓库根目录执行
uv sync --all-extras --locked
./.venv/Scripts/python.exe -m chainscope --help
./.venv/Scripts/python.exe -m chainscope init-db
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check python
```

## 后续约定

- 新 Python 文件只放 python 下已有职责目录，各目录不再嵌套。
- SQL、Markdown、CSV、JSON、日志、IDE 配置各归其类；说明文档不放进代码或数据目录。
- 采集数据按批次嵌套一层，不另建逐交易子目录。
- 本次仅整理目录与引用。测试范围内的精度、事务和去重已经验证；业务所需的覆盖证明、恢复、容量和持续运行仍需另行实现并验收。
