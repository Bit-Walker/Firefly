# 命令行客户端

`edbg_cli` 通过 Python SDK 访问 HTTP 接口。提供健康、节点、能力、接口描述与任务查询，追加目标/连接配置，以及不执行硬件动作的 operation plan。配置 apply 会写入服务端注册表。

```powershell
.\tools\edbg.ps1 health --json
.\tools\edbg.ps1 node list --credentials-file fleet/.state/admin.local.json --json
```

从仓库根目录执行；先运行 `tools/bootstrap.ps1`。也可以使用 `.venv/Scripts/edbg.exe`，或相应环境的 `python -m edbg_cli`。

参数、认证、退出码与机器输出见 [CLI 参考](../../docs/reference/cli-query-v0.md)。架构讨论中的动作命令不属于当前 CLI 的已实现功能。

配置、修订冲突与预检查说明见 [工作空间接口](../../docs/reference/workspace-v0.md)。

已增加 operation submit/get/list/events/cancel 及 --wait，UART 子集和有限 I²C 事务会执行真实硬件动作。机器输出、等待失败与兼容边界见 [UART 参考](../../docs/reference/uart-operations-v0.md)及 [I²C 参考](../../docs/reference/i2c-operations-v0.md)。

`stream list/show/read/follow` 提供 RX 读取。follow 为有限 NDJSON 输出，结束或断网时保留检查点，不续租会话；见 [接收流参考](../../docs/reference/uart-streams-v0.md)。

`session list/show` 提供只读租约快照，uart.session.renew 通过现有操作提交；修订、幂等与旧控制规则见 [续租参考](../../docs/reference/uart-leases-v0.md)。

`artifact list/show/import --image` 提供节点镜像目录，导入不触发 OTA。node.update 通过相同 operation 命令提交，要求健康新启动和完整运行镜像摘要，不支持弱确认回退。见 [镜像与 OTA 参考](../../docs/reference/node-update-v0.md)。
