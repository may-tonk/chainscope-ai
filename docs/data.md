# 样本与证据

- `samples/sample_transfers.csv`：保留的 20 条真实历史记录，samples 只放 CSV。
- `reports/sample_source.json`：样本来源与核验范围说明，reports 只放 JSON。
- `raw/<batch-id>/`：保留历史归档；新批次仅在 --save-raw 或 fetch 调试时生成 RPC 响应和摘要，均为 JSON，只嵌套一层。collect 默认不写文件，新采集不生成 events.json。
- `sql/migration-0001.sql`：已有初始迁移 SQL 导出，不作为采集结果。

结构化事件与批次正式存储在 MySQL；这里的文件用于样本、证据和排错。MySQL 物理数据目录由数据库部署配置管理，不能放进源码仓库。

调试归档默认单批 10 MiB、整个 raw 文件内容总量最多 100 MiB，旧文件也计入；超限报错，已有内容不自动清理。原始响应不是数据库备份，正式事件和批次以 MySQL 为准。自动到期清理和回放未实现。
