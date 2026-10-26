# 当前命令行查询接口

状态：已实现的兼容查询入口，2026-10-27。输出包络版本为 `edbg.cli.query/v0`，与架构方案中尚在讨论的统一动作接口分开演进。

本页记录第一批查询入口。后续已增加 [目标/连接配置及 operation plan](workspace-v0.md)，另已支持 [UART 兼容执行](uart-operations-v0.md)。冲突或执行前条件不满足使用退出码 7；操作等待到期为 8，等待到非成功终局为 9。预检查需读取 data.status。[接收流](uart-streams-v0.md)的过期游标退出码为 10，错误 context 提供显式恢复位置。stream follow 使用独立 NDJSON 协议。

现已通过相同 operation 命令支持 [有限 I²C](i2c-operations-v0.md)和 [节点更新](node-update-v0.md)。artifact list/show 为目录查询，artifact import --image 为镜像登记，会写服务存储，但不触发更新。目录分页使用 --before/--limit。

## 安装与调用

在仓库根目录执行 `tools/bootstrap.ps1`，然后使用 `tools/edbg.ps1`。也可调用项目环境中的 `edbg` 或 `python -m edbg_cli`。

| 命令 | 结果 |
| --- | --- |
| `edbg health` | 控制服务健康状态，无需登录 |
| `edbg node list` | 节点摘要列表 |
| `edbg node show <id>` | 单个节点摘要 |
| `edbg capability list --node <id>` | 节点报告的能力、上线状态及匹配的兼容接口描述 |
| `edbg profile list` | 当前服务的接口描述 |
| `edbg job list [--node <id>]` | 最近任务，可按节点筛选；当前服务最多返回最近 200 项 |

上述命令不发起 OTA、重启、串口发送或总线事务。认证时会建立登录会话。

## 连接和认证

通用参数可放在命令前后：

| 参数 | 默认与含义 |
| --- | --- |
| `--url <origin>` | `EDBG_URL`，未设置时为 `http://127.0.0.1:8088`；只接受 HTTP(S) 服务根地址 |
| `--credentials-file <path>` | `EDBG_CREDENTIALS_FILE`；未提供时不登录，不自动读取服务端私有文件 |
| `--timeout <seconds>` | 单次 HTTP 请求超时，默认 15 秒，必须是有限正数 |
| `--json` | 标准输出为一个结果或错误 JSON 对象 |

凭证文件是含 `username`、`password` 字符串的 JSON。本机兼容管理员文件可显式指定为 `fleet/.state/admin.local.json`；复制或分享命令输出时不附带此文件。HTTPS 使用正常证书校验。

```powershell
.\tools\edbg.ps1 node list --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 capability list --node s3-设备标识 --credentials-file fleet/.state/admin.local.json --json
```

当前认证沿用单管理员 API，尚未实现人类与 AI 独立授权身份。

## 输出和错误

```json
{"schema":"edbg.cli.query/v0","ok":true,"data":{"ok":true}}
```

```json
{"schema":"edbg.cli.query/v0","ok":false,"error":{"code":"AUTH_REQUIRED","message":"Control service returned HTTP 401.","http_status":401}}
```

包络定义在 `contracts/compat/fleet-v1/cli-query.schema.json`。节点摘要仅输出身份、名称、分组、在线状态、心跳、启动标识、版本和报告的能力，省略兼容 API 的内部存储字段。

| 退出码 | 错误类型 |
| --- | --- |
| 0 | 成功 |
| 2 | `INVALID_ARGUMENTS` 或 `INVALID_CONFIG` |
| 3 | `AUTH_REQUIRED`，包括当前 API 的 HTTP 401/403 |
| 4 | `CONNECTION_FAILED` |
| 5 | `NOT_FOUND` 或 `HTTP_ERROR` |
| 6 | `INVALID_RESPONSE`，无法解析或不符合查询需要的响应 |

默认便于人工阅读的输出仍为格式化 JSON；错误写入标准错误。`--json` 模式下，业务错误写入标准输出并以非零码退出。`--help` 沿用普通帮助文本，不能当作 JSON 查询调用。

`reported` 能力列表来自最近一次设备上报，离线时也可能保留；接口描述来自当前服务配置。二者都不能单独证明一个动作现在可执行。完整的有效能力、资源租约、动作、事件与产物契约仍在设计中。

`session list/show` 查询受管上下文与服务租约；续租继续使用 operation submit 文件入口。LEASE_REVISION_CONFLICT 和 LEASE_NOT_EXTENDED 使用退出码 7，详见 [续租参考](uart-leases-v0.md)。
