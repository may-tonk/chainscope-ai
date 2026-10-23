# Navicat 只读查看与核对

更新日期：2026-10-19。在 Navicat 选择项目配置指向的数据库后执行；密码只放在本地连接配置和 `.env`。

`sql/acceptance.sql` 保留早期批次、事件身份与关联核对，不代表覆盖了长期任务和 Agent 的全部验收。最早结果属于 2026-10-12 历史证据，当前状态以数据库为准。

## 已入库连续进度

```sql
SELECT job_id, verified_through AS transfers_through, balances_through
FROM scan_jobs WHERE job_id = 'bem';

SELECT job_id, verified_through AS swaps_through
FROM supplement_jobs WHERE job_id = 'bem';

SELECT job_id, scanned_through AS pools_through
FROM pool_discovery_progress WHERE job_id = 'bem';
```

进度表示完成到哪里，不是事件总数；未采集或未核验的范围不能解释为没有事件。

## 运行与结束报告

```sql
SELECT run_id, state, accepted_at, heartbeat_at
FROM agent_runs WHERE token_id = 'bem'
ORDER BY accepted_at DESC LIMIT 20;

SELECT event_id, run_id, kind, occurred_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.report.state')) AS report_state
FROM agent_events
WHERE kind IN ('run_finished', 'run_unconfirmed')
ORDER BY event_id DESC LIMIT 20;
```

数据库时间列为 UTC，对话展示北京时间。`completed` 要求四项覆盖达到固定目标，报告 `displayed` 只表示程序完成输出和记账。

二进制指纹可通过 `HEX(fingerprint)` 只读查看，无需更改字段类型。这些 SQL 用于本地人工管理，Agent 不开放任意 SQL 工具。
