# 项目文档

主线是一个可由人和 AI 共用的远程多节点开发、调试、采集与分析系统。当前实现与规划分别维护，接口变化直接更新相应文档。

| 文档 | 用途 |
| --- | --- |
| [当前状态](current-state.md) | 已有能力、实际限制、当前验证依据 |
| [架构](architecture/README.md) | 目标、领域对象、执行和数据边界 |
| [研发顺序](architecture/08-next-implementation-batch.md) | 下一项要解决的问题及完成条件 |
| [开发指南](guides/development.md) | 目录、运行、验证、私有状态和分发 |
| [运行信息协议](reference/agent-runtime.md) | 节点事实、镜像测量和版本化能力 |
| [镜像与 OTA](reference/node-update-v0.md) | 登记、发布、维护互斥与严格确认 |
| [连接与计划](reference/workspace-v0.md) | 配置修订、来源及执行前校验 |
| [CLI](reference/cli-query-v0.md) | 命令、机器包络和错误 |
| [UART](reference/uart-operations-v0.md) | 控制会话、发送、幂等及结果 |
| [UART 流](reference/uart-streams-v0.md) | 独立读者、游标、缺失与保留 |
| [租约](reference/uart-leases-v0.md) | 显式续期与控制失效 |
| [I²C](reference/i2c-operations-v0.md) | 有限事务、清理和字节进度证据 |

当前设计以这些入口为准；早期方案与验证材料不作为现行功能承诺。新增工作直接更新当前文档，避免再维护逐次开发记录。
