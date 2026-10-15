# 运行与验收手册（BEM 长期采集）

日期：2026-10-15。命令都在项目根目录的 PowerShell 中执行。下文 `cs` 指 `.\.venv\Scripts\python.exe -m chainscope`，也可以换成 `uv run --all-extras --locked chainscope`。

## 1. 首次准备

```powershell
cs init-db                      # 升级到迁移 0004，只新增表、列和索引
cs check                        # 包括与 models.py 的完整结构比对，schema_differences 应为空
cs job-create --job bem --token 0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a
```

`job-create` 会用 eth_getCode 二分查找部署区块（约 30 次请求），并读取 decimals。

## 2. 日常运行

```powershell
cs sync --job bem --max-minutes 30 --trace-memory   # 回填；可以随时 Ctrl+C，下次自动续采
cs status --job bem --max-lag-blocks 2000          # 落后超过约 15 分钟时退出码为 2
cs query --job bem --from 2026-10-01 --to 2026-10-08
```

- `sync` 默认每个窗口都用 Alchemy 对账（条数、金额合计、指纹），并随机抽 3 笔交易核对回执；对账不一致时，差异写入 `window_diffs`，命令以非 0 退出，水位不会越过这个窗口。
- 预算参数：`--max-minutes`、`--max-requests`、`--max-cu`、`--max-windows`。到达上限后在窗口之间停下，下次接着做。
- 每个窗口的进度记录在 `logs/sync-YYYYMMDD.jsonl`。

## 3. 定时运行

```powershell
powershell -ExecutionPolicy Bypass -File python\tools\register_sync_task.ps1
```

注册后每 10 分钟运行一次 `sync --max-minutes 9`。任务计划程序不会重叠启动；同时 MySQL `GET_LOCK` 保证同一任务只有一个进程。电脑休眠或重启后，会在下一个时间点补跑。删除定时任务：`Unregister-ScheduledTask -TaskName "ChainScope BEM sync" -Confirm:$false`。

## 4. 复查与重做

```powershell
cs recheck --job bem --sample 20      # 随机重新拉取已完成窗口；有变化就标为失败并回退水位，退出码 3
cs redo --job bem                     # 把旧解析版本的窗口重置为待采集
cs redo --job bem --from-block X --to-block Y
cs verify-job --job bem               # 从 MySQL 重新计算每个窗口，与保存的指纹比对
```

## 5. 备份与恢复演练

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
foreach ($point in "after_fetch", "before_commit", "after_commit") {
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
| 22 | 内存峰值 | `sync --trace-memory` 的 `memory_peak_mib` | |
| 51 | 存储 | `cs report` 的 tables | |
| 52、53 | 查询耗时与索引 | `cs report` 的 queries；在 sync 运行时再测一次 | |
| 54 | 迁移耗时 | `Measure-Command { cs init-db }`（升级前先备份） | |
| 55 | 备份恢复 | 第 5 节 | |
| 56、65 | 持续运行 | 第 3 节运行 48 小时，期间 `status` 的 behind 都为 false | |
| 60、62 | 代表性窗口 | 选最密集的一天、空范围、铸币/销毁、一笔多日志的范围，用 `redo` 重做并对账 | |
| 61 | 全量回填 | 从部署区块 sync 到 caught_up，记录 elapsed_seconds 和 usage | |
| 63 | 强退恢复 | 第 6 节 | |
| 64 | 长时间停机 | 停 1 天后再运行 sync，确认自动补齐 | |
