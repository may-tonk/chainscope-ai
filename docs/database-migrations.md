# 数据库迁移

存放 MySQL 表结构的版本化迁移。初始 `0001` 已在真实 MySQL 8.0.43 执行，重复运行成功。

2026-10-13 新增并执行 `0002_optional_archive.py`，仅将 collection_batches.raw_directory 改为可空。默认 collect 无归档时保存 SQL NULL；旧批次路径、事件和关联保持完整。数据库初始化执行到最新版本，不改写历史 0001。

`python/migrations/0001_initial.py` 冻结三张业务表的初始定义：collection_batches、transfer_events、batch_events。迁移入口在 `python/migration_env/env.py`，alembic.ini 用 script_location 和 version_locations 指定两个并列目录，避免三层嵌套。用项目 CLI `init-db` 创建指定数据库并执行 Alembic。后续变更添加新版本，保留已执行版本；不要编辑已执行迁移去改变现有数据库。破坏性 downgrade 被禁用。

Navicat 用于连接数据库和检查结果；正式结构变更必须保留迁移文件和验证记录。

迁移目标为 MySQL，计划使用 InnoDB、utf8mb4 和 Alembic。不要直接复用其他数据库的金额类型或冲突写入语法：MySQL DECIMAL 最大精度为 65 位，而完整 uint256 可以有 78 位十进制数字。原始金额需要用规范十进制字符串或 32 字节二进制完整保存；金额聚合时再明确可用数值范围或使用 Python 精确整数计算。

依据：[MySQL DECIMAL 文档](https://dev.mysql.com/doc/refman/8.0/en/precision-math-decimal-characteristics.html)。`VARCHAR(78)`、数字格式和 uint256 上限 CHECK、事件唯一键已通过真实数据库测试。

2026-10-15 新增 `0003_jobs_windows.py`：新建 scan_jobs、coverage_windows、window_events 三张表，并给 collection_batches 增加可空的 job_id 列，用于长期采集任务的窗口进度与续采。只新增表和可空列，不改动已有数据；`check` 要求结构版本为 0003，已有数据库执行一次 `init-db` 即可升级。
