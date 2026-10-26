# 受管 UART 显式续租

状态：已实现 v0，2026-10-27。续租是操作账本中的 uart.session.renew 动作，显式更新服务端租约期限，不由读取、订阅、登录刷新或普通写入自动触发。

## 查询会话

```powershell
.\tools\edbg.ps1 session list --node <节点ID> --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 session show <会话ID> --credentials-file fleet/.state/admin.local.json --json
```

只列出受管 UART 会话。有限分页使用 --limit 和 --before。查询纯只读，返回 edbg.session/v0：固定连接绑定、控制器存储状态、RX stream_id、服务租约与最近上下文快照。

lease.revision 是服务租约修订，不是 operation.revision，也不是设备已确认的 fencing 编号。lease.expires_at 为服务端 Unix 时间，remaining_ms 只表示期限剩余；必须同时检查 state，关闭会话可能仍保留未来的历史期限。

node.control_heartbeat_fresh 采用执行控制的 8 秒心跳门限，不替代节点列表的在线状态。last_context_snapshot 是最近保存的上下文信息，不能脱离 state、启动标识与时间判断物理接口仍活动。

## 提交续租

先读取当前 lease.revision，并保存一个包含新随机幂等键的请求文件：

```json
{
  "api_version": "edbg.operation.draft/0",
  "connection": {"id": "uart-NODE_ID", "revision": 1},
  "action": "uart.session.renew",
  "params": {
    "session_id": "实际会话ID",
    "expected_lease_revision": 1,
    "lease_duration_ms": 60000
  },
  "idempotency_key": "REPLACE_WITH_A_NEW_RANDOM_KEY",
  "timing": {"dispatch_within_ms": 5000, "execution_timeout_ms": 10000}
}
```

```powershell
.\tools\edbg.ps1 operation plan --file renew.json --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation submit --file renew.json --wait --credentials-file fleet/.state/admin.local.json --json
```

新期限为接受时刻加 lease_duration_ms，不是在旧期限上累加。每次请求 100–300000 ms，且新期限必须晚于当前期限；否则 LEASE_NOT_EXTENDED，退出码 7。因此剩余 4 分钟时请求 1 分钟不会缩短或“续满”它。

expected_lease_revision 必须匹配当前值。不同键并发续租只有一个可成功；另一个返回 LEASE_REVISION_CONFLICT，退出码 7。调用者先核对原操作，再决定是否读取新修订、创建新的明确意图，不自动忽略冲突。

相同键和相同请求始终返回原操作及当时的期限，不再次延长。即使会话后来关闭或到期，重试原键也不会重新激活它；原续租记录的 succeeded 是历史事实，不表示会话现在仍有效。

## 接受与证据

续租要求：受管会话、同一主体、匹配连接修订、当前连接有效、节点启动匹配、控制心跳新鲜、固件能力可用，且会话当前为 active、尚未到期。关闭中、已关闭、已到期、旧启动或旧浏览器会话均不能续租。

期限修改、租约修订递增、操作与事件同一数据库事务提交，失败全部回滚。结果立即 finished/succeeded，记录前后修订及期限：

- result.confirmation=server_policy_only。
- result.node_revision_acknowledged=false。
- evidence.level=server_policy。
- result.session_resources_held=true；该次策略操作本身没有持续占用，resource_status=released 不代表 UART 会话已释放。

旧固件只接收现有不超过 10 秒的局部控制租期，不回传服务修订，也没有新增逐操作预算或节点 fencing 协议。本次成功只证明服务策略已提交；节点仍需通过后续正常轮询保持实际控制，不能把它当成设备已接受指定修订的证据。

续租不重开串口、不创建新 RX 流、不重置接收序号，不修改已有写入的派发/确认期限，也不恢复未知写入。未知写入仍阻挡同一上下文的新写入；明确续租只允许继续保留当前有效控制上下文以核对结果。

续租提交即完成，取消该已完成操作只返回原结果，不能倒退期限。需要停止时使用 uart.session.close；关闭不要求匹配新的租约修订，且可使用会话原始连接修订执行清理。

## 旧控制请求失效

对于已经确认 active 的受管会话，节点后续快照缺失、会话不匹配或不再是预期活动上下文时，服务停止发送旧控制与新字节，不自动重开旧 session_id。

节点明确报告空闲时可结束上下文；其他不确定情况先进入 closing，继续保留资源占用，等待后续结束证明。排队而未派发的字节被停止，已派发但未确认的字节保留未知结果。迟到的活动快照不能把 closing 改回 active。

这个规则只收紧新受管接口，旧浏览器接口保留原兼容行为。它也不等于节点能够拒绝所有延迟网络响应；完整的设备侧控制代次确认仍需后续代理协议升级。

## API 与 SDK

| 方法与路径 | 用途 |
| --- | --- |
| GET /api/workspace/sessions?node=ID&before=ID&limit=N | 受管会话列表 |
| GET /api/workspace/sessions/{id} | 只读租约及上下文快照 |
| POST /api/workspace/operations | 提交显式续租，与其他 UART 动作共用账本 |

SDK 新增 sessions 与 session；提交仍使用 submit_operation。未找到受管会话返回 SESSION_NOT_FOUND，退出码 5。请求样例位于 contracts/drafts/operation-v0/fixtures/uart-renew.json，会话响应契约位于 contracts/compat/uart-session-v0/session.schema.json。

迁移 0004 为现有受管会话添加 lease_revision，初始值 1，不改变既有期限。历史关闭会话保留这个值，并不会获得新的活动控制权。
