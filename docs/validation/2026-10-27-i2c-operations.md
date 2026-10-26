# 有限 I²C 操作验证

日期：2026-10-27。交付：i2c.transfer 持久执行、临时上下文、全局适配器互斥、取消和自动清理。使用范围见 [接口参考](../reference/i2c-operations-v0.md)。

## 软件验证

81 项机器客户端/集成测试通过，本批新增 15 项；44 项原服务端回归通过。覆盖：

- 单写、单读和写后读的模拟完成、响应契约与清理后终局。
- 幂等重试、十六进制归一及不同意图同键冲突。
- NACK、超时、非空闲线路、报告长度不符与不确定字节进度。
- 多节点并发只允许一个适配器持有者，换 physical_bus_id 不能绕过。
- 旧创建、取消与上下文关闭接口不能绕过新占用。
- 主机完成但释放未确认时保持全局占用；后到证明修订原结果。
- 丢失派发响应、节点重启、独立服务进程恢复后不再次发布。
- 准备前、准备后与总线发布后取消的不同副作用边界。
- 接受事务中断回滚上下文、操作及全局占用。

证据：本机 `artifacts/i2c-operations-tests.log`、`artifacts/i2c-operations-regression-tests.log`。成功 ACK/读回及模拟故障不被表述为真实外设实测。

## 实板验证

两板维持固件 3.5.0、现有 5 cm 接线和内部上拉。选择 B 的 I²C 连接，对 0x55 只发布一次单字节读取，不扫描、不自动重试。

操作 `2dfaca8955f0ba4d8d4e60fe` 收到 I2C_NACK；主机报告 SDA/SCL 空闲值均为 1，耗时 1843 µs。没有返回伪造读数据，读回确认数为未知。CLI --wait 返回非成功终局码 9，整体 outcome=failed。

观察到 releasing 阶段后，节点确认临时上下文释放；最终 resource_status=released、cleanup_confirmed=true、全局 held=0。原键重试返回原操作，仅一条关联 i2c_runs 记录，没有遗留活动调试上下文。

证据：`artifacts/i2c-operations-live-check.json`、`artifacts/live-i2c-operation-request.json`。当前未连接外部应答器，尚未在新接口实测成功 ACK 或外设数据。

## 迁移与分发

迁移 0005 前后旧节点、任务、固件、调试、I²C、操作、租约和接收流数量一致。实测随后新增一个操作、一个内部上下文和一个关联事务，不创建用户 UART 租约或 RX 流。

私有备份：`fleet/.state/pre-i2c-operations.local.bak` 与 `migration-0005-*.local.bak`。上一批公开源码：`artifacts/pre-i2c-operations-source.zip`。当前分发包检查见 `artifacts/i2c-operations-package-check.json`。

旧服务会拒绝新数据库版本；不能删除迁移历史强行回退。恢复前需核对现场副作用、保留当前数据，不能将旧备份中缺失的发布记录当作重新执行的依据。

本轮不升级或重启开发板，没有改网页或重复浏览器 UI 测试。Docker 镜像构建与公网部署仍未验证。
