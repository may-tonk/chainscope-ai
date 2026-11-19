"""Memory 业务迁移目录，预留独立 Alembic 入口与 versions 版本脚本。

后续仅管理 Memory 所属业务结构，与账户迁移、采集 MySQL 迁移分开，
官方 checkpoint 内部迁移继续由其显式初始化器维护。
实现迁移时需要登记配置、版本表、数据库身份与运行/迁移账号权限。
已有 env.py、alembic.ini 的 [memory] 入口和 mem0001 会话迁移，
版本表为 research.memory_alembic_version；结构变更通过新增版本推进。
导入此包不连接数据库、不执行 DDL，也不更改已有迁移水位。
"""
