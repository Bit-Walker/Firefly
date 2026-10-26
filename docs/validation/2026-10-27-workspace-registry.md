# 连接注册表与预检查验证

日期：2026-10-27。此为配置批次的阶段记录，后续 UART 执行见 [本批验证](2026-10-27-uart-ledger.md)。交付：目标/连接版本注册表、编号数据库迁移、当前固件兼容评估、SDK 和 CLI。使用方法见 [接口参考](../reference/workspace-v0.md)。

## 结果

| 范围 | 结果 |
| --- | --- |
| 原服务端回归 | 44 项通过 |
| 机器客户端及新增集成 | 23 项通过，临时数据库与真实服务 API 适配，无实板动作 |
| 并发更新 | 同一预期修订的两个请求仅一个成功，另一请求冲突 |
| 编号迁移 | 并发只应用一次；备份保持迁移前内容；失败完整回滚 DDL 和账本；变更/未来迁移历史拒绝启动 |
| 台架导入 | 只导入一次；模拟中断回滚部分记录与标记，不覆盖已有修订 |
| 预检查无副作用 | 预检查前后临时数据库完整 SQL 快照一致 |
| 状态与范围 | 离线、启动变化、旧控制上下文、错误参数、缺失绑定、外来会话及未知适配器均有机器原因 |
| 输入 | 重复键、非有限数值、浮点数、未知字段和超大请求被拒绝 |
| 本机切换 | 迁移前无活动任务/会话；原双端口恢复，两节点继续在线 |
| 联机预检查 | UART 与 I²C 均为 supported，reserved=false、submission_available=false |

切换前后主记录数量相同：2 个节点、26 个任务、10 个固件、46 个调试会话、13 个串口发送、34 个 I²C 记录和 13 个波形记录。新增 3 个目标、6 条连接、一次迁移和一次台架导入记录。

这次没有执行 OTA、设备重启、UART 发送、I²C 事务或波形动作。没有重跑浏览器 UI 流程。

## 证据与恢复

本机证据：

- `artifacts/workspace-tests.log`：23 项机器客户端/集成验证。
- `artifacts/workspace-regression-tests.log`：44 项原服务回归。
- `artifacts/workspace-bootstrap.log`：项目包安装。
- `artifacts/workspace-live-check.json`：联机状态、记录数量与预检查摘要。
- `artifacts/workspace-live-uart-request.json`、`workspace-live-i2c-request.json`：本机有效预检查请求。
- `artifacts/workspace-package-check.json`：分发包与全新解压目录验证。

上批公开源码包保留为 `artifacts/pre-workspace-registry-source.zip`；切换前数据库备份为私有 `fleet/.state/pre-workspace-registry.local.bak`，迁移另生成 `migration-0001-*.local.bak`。

本次仅新增表，回退前一版服务源码可保留新增表并停止使用它们。需要数据库整体恢复时必须停服务、先备份当前数据；整体恢复会舍弃备份后的所有记录，不应作为常规源码回退步骤。旧源码包恢复后，应重新安装本地可编辑包再启动。

## 实际限制

本批完成配置与预检查入口，不等于完成统一操作系统。尚未提供操作提交、账本、资源租约、节点防重放新协议或独立 AI 授权。禁用新连接不撤销旧调试入口。

固件身份、固定接口及关联外设限制继续保留；已有 UART 会话无法证明连接修订绑定。外部烧录与要求镜像身份确认的新节点更新请求均不支持。supported 只表示兼容快照检查通过，不是实际接线或执行成功证明。

Compose 配置可检查；本机 Docker 引擎未运行，镜像构建和公网部署仍未验证。
