# 测试与真实 MySQL 验证

更新日期：2026-10-19。默认使用模拟来源；真实 MySQL 和 DeepSeek 分别显式启用。

## 默认测试

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q
```

默认不访问真实链上来源，也不启用真实 MySQL 和付费模型测试。场景包括分页、整数金额、事务、隔离、恢复、时间边界、授权范围、通知和报告。模拟数据不作为正式采集结果。

## MySQL 测试

```powershell
$env:CHAINSCOPE_TEST_MYSQL='1'
.\.venv\Scripts\python.exe -B -m pytest -q
Remove-Item Env:/CHAINSCOPE_TEST_MYSQL
```

`python/tests/conftest.py` 创建随机命名的 `chainscope_test_*` 数据库，先确认不存在，结束后仅删除自己创建的库。账户需要相应建库和删库权限；不清空正式库，不在正式库降级结构或注入故障。

Agent 集成测试覆盖迁移兼容、并发启动、真实采集代码与模拟来源衔接、固定时间边界、结束事件和报告处理权。不要并行运行使用同名任务全局互斥锁的 MySQL 测试套件。

## 可选真实模型检查

`CHAINSCOPE_TEST_DEEPSEEK=1` 会启用真实模型请求，可能产生模型费用；需要同时开启 MySQL 测试并配置密钥。使用临时库和模拟采集来源，不启动正式历史采集。常规文档或缓存整理无需反复执行它。

## 已有证据

2026-10-19 完整回归记录为 358 项通过、2 项可选模型测试跳过、5 项 subtests 通过。真实 DeepSeek 单条指令启动和自动报告另行通过；随后 BEM 正式增量完成到指定时间，见 [README](../README.md#测试与验证证据)。证据分别适用于对应场景，不代替所有代币、来源和持续运行验收。
