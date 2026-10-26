# 持久 UART 操作接口

状态：已实现的兼容子集 v0，2026-10-27。提供受管 UART 会话打开、有限写入、关闭及显式续租，以及操作查询、事件、取消与幂等核对。[有限 I²C 事务](i2c-operations-v0.md)和 [节点更新](node-update-v0.md)复用相同操作入口。波形与外部烧录尚未接入这套统一执行接口。

## 运行边界

当前使用 edbg.agent/v1 的 uart.fixed/1 驱动及固定 UART 接线。新旧入口共用 debug_sessions/debug_tx，互相遵守现有互斥；新入口不会额外取得同一节点的第二份控制权。受管会话保留原始连接修订、节点启动与适配器快照，旧调试入口不能关闭或发送到该会话。

认证仍为单管理员。操作主体固定为本工作空间的 local-admin，登录 cookie 只负责认证，因此重新登录可以继续查询和控制原受管会话；这不代表已具备独立的人类、AI 或多用户授权。

操作响应的 compatibility 明示兼容等级 legacy_uart_once：

- UART 写入只发布一次，服务不自动重发。未送达与已执行但丢失确认，都可能最终成为 unknown。
- execution_timeout_ms 在此适配器中是从服务发布响应起算的完成确认窗口；尚未向节点传递逐操作执行预算。未来通用节点预算语义仍待代理协议升级。
- 节点继续使用现有不超过 10 秒的本地控制租期与固件内置的驱动完成机制。这里不承诺掉电持久去重或物理动作恰好执行一次。
- 成功证据最多是 UART 驱动报告完成，不证明接收方执行了业务命令。需要接收确认时读取对端数据或另外定义目标协议。

初始会话租期从接受时起算，最多 300 秒，包含等待节点的时间；后续可使用 [显式续租](uart-leases-v0.md)，每次新的期限也不超过接受时刻后 300 秒。CLI 订阅或登录刷新不会续期。探针板的旧调试上下文仍关联 UART2 监听，具体资源包含在计划中。

## 请求文件与 CLI

创建 open.json，填入 connection list 得到的实际 ID 和当前修订；为每个新意图生成一个新随机幂等键，并在首次发送前保存文件。

```json
{
  "api_version": "edbg.operation.draft/0",
  "connection": {"id": "uart-NODE_ID", "revision": 1},
  "action": "uart.session.open",
  "params": {
    "baud_rate": 115200, "data_bits": 8, "parity": "none",
    "stop_bits": "1", "flow_control": "none", "lease_duration_ms": 60000
  },
  "idempotency_key": "REPLACE_WITH_A_NEW_RANDOM_KEY",
  "timing": {"dispatch_within_ms": 5000, "execution_timeout_ms": 10000}
}
```

```powershell
.\tools\edbg.ps1 operation submit --file open.json --wait --wait-timeout 15 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation get <操作ID> --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation events <操作ID> --after 0 --limit 100 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation list --node <节点ID> --limit 50 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation cancel <操作ID> --credentials-file fleet/.state/admin.local.json --json
```

打开后取得 session_id，并等待 outcome=succeeded 再写入。新打开操作的 result.stream_id 指向受管 RX 流，也可通过 stream list 按会话查询。写入文件使用相同 connection 快照、action=uart.write、params 为 session_id 与 data_hex；execution_timeout_ms 不超过 10000，字节数不超过 256。原始字节不自动附加换行，也不自动按文本编码转换。

关闭文件使用 action=uart.session.close、params={"session_id":"实际身份"}，并使用新的幂等键。关闭可引用会话原始连接修订，即使连接后来禁用或改变；清理也允许在节点离线时提交，停止后续控制续期，但资源释放仍需节点报告确认。

write 必须引用通过新接口建立且连接修订匹配的受管会话。不能将旧浏览器会话直接提升为新受管上下文。open/close 的确认窗口不超过 30 秒；write 不超过 10 秒。命令行 --wait-timeout 与这些期限独立。

## 幂等与恢复

唯一作用域是当前工作空间的主体和 idempotency_key。相同规范请求返回原操作；不同请求使用同键返回 IDEMPOTENCY_CONFLICT，退出码 7。规范化版本 strict-json-uart/1 拒绝重复键、浮点数、非有限数值和未知字段，按键排序并将 UART 十六进制转为小写。

同键重试先核对持久记录，再检查当前连接，因此配置改动不会把原意图绑定到另一个版本，也不会把原操作重复执行。已接受操作使用固定绑定；禁用连接只阻挡后续新意图，不撤销已经接受的操作。需要停止它时显式取消或关闭会话。

请求文件保留原键。提交响应丢失时重试原文件，不生成新键。操作与会话建立/发送记录同一事务保存，发布标记也与设备轮询事务共同提交；进程重启后继续查询原账本。

资源计划目前由旧会话互斥、旧发送队列及受管绑定落实，尚未实现通用资源租约调度器。未知写入会阻挡同一受管会话的新写入；可以等待迟到证据，或显式关闭并确认旧上下文结束，再决定新的业务意图。failed 且 effect=possible 同样不代表可以安全重复业务命令。

## 状态、结果与事件

实际操作阶段为 accepted、dispatched、finished。dispatched 表示控制或发送已放入服务响应，不证明节点收到。结果为 succeeded、failed、cancelled、expired 或 unknown；当前旧驱动没有可靠的部分字节计数，因此不虚构 partial 或 confirmed_bytes。

写入 succeeded 时，confirmed_bytes 表示驱动报告的发送数量；驱动错误或未知结果时为 null，effect=possible。会话 active 表示节点报告接口就绪；关闭成功表示节点报告结束，或已识别新启动使旧上下文失效。

resource_status 与 outcome 分开：写入结束可释放单次发送占用，而会话继续持有 UART。会话打开操作通常保持 held，直到节点确认关闭或新启动。未知结果不能自动把资源标为已释放；迟到证据可修订原结果，并追加递增 revision/事件序号。

GET 操作、列表、事件会持久保存到期与已有证据的核对结果，但不会发布硬件命令。operation plan 仍为不写数据库的预检查。操作事件当前不清理，after 为上次事件序号，返回 next_after/has_more；列表用 next_before 游标分页。操作事件尚无 NDJSON watch；UART RX 已另提供 [流读取与跟随](uart-streams-v0.md)，原接收接口继续兼容保留。

## 取消与客户端等待

- 未发布的打开/写入可取消，确认无发送后返回 cancelled。
- 已发布的打开取消会停止继续打开/续期，等待节点释放确认。
- 已发布的 UART 写入无法可靠撤回；取消记录意图，实际完成证据优先，可能最终成功或未知。
- 关闭意图不支持反向取消。重复取消已结束操作只返回原结果。
- --wait 超时返回原操作身份及 wait_timed_out=true，退出码 8，不取消设备动作。
- --wait 查询断网时仍返回已接受操作身份和 wait_error，使用对应非零码；原意图仍可核对。
- --wait 得到非成功终局时退出码 9；不带等待的 submit 和普通 get 成功读取结果时退出码 0。

机器调用必须同时判断命令退出码、operation.outcome、effect 和兼容证据级别，不能把 HTTP 200 或包络 ok=true 当成目标业务成功。

## API 与 SDK

| 方法与路径 | 用途 |
| --- | --- |
| POST /api/workspace/operations | 接受 UART、有限 I²C 或兼容节点更新意图 |
| GET /api/workspace/operations?node=ID&before=ID&limit=N | 分页列表 |
| GET /api/workspace/operations/{id} | 结果与修订 |
| POST /api/workspace/operations/{id}/cancel | 有边界的取消意图 |
| GET /api/workspace/operations/{id}/events?after=N&limit=N | 递增事件 |

SDK：submit_operation、operation、operations、operation_events、cancel_operation、wait_operation。wait_operation 返回等待状态，不打印进度；接口不自动产生新的幂等键。

数据库迁移 0002 添加账本、事件和会话绑定。原有数据及旧调试功能保留，迁移前自动在线备份。部署升级需要同时更新两个 API 进程，不能混用不认识受管会话的旧服务进程。
