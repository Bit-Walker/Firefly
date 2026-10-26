# 有限 I²C 操作接口

状态：已实现兼容子集 v0，2026-10-27。i2c.transfer 接入持久操作账本、幂等核对、取消与事件，支持单写、单读和写后读。当前适配器为edbg.agent/v1 的 i2c.fixed/1 驱动，固定 GPIO9/10 主机，10 kHz、内部上拉，每方向最多 64 字节。

## 提交请求

先使用 connection list 找到 I²C 连接身份和当前修订，保存一个新意图文件：

```json
{
  "api_version": "edbg.operation.draft/0",
  "connection": {"id": "实际I2C连接ID", "revision": 1},
  "action": "i2c.transfer",
  "params": {
    "address": 118,
    "speed_hz": 10000,
    "messages": [{"write_hex": "d0"}, {"read_length": 1}]
  },
  "idempotency_key": "REPLACE_WITH_A_NEW_RANDOM_KEY",
  "timing": {"dispatch_within_ms": 10000, "execution_timeout_ms": 10000}
}
```

地址和写入字节必须按实际外设协议设置；示例不证明目标存在或这些字节适合任意外设。单写仅放 write_hex，单读仅放 read_length。组合消息间重复 START，最后 STOP；不悄悄拆成多个带 STOP 的请求。

```powershell
.\tools\edbg.ps1 operation plan --file transfer.json --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation submit --file transfer.json --wait --wait-timeout 35 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation get <操作ID> --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation events <操作ID> --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation cancel <操作ID> --credentials-file fleet/.state/admin.local.json --json
```

SDK 沿用 submit_operation/operation/wait_operation/operation_events/cancel_operation，不增加客户端硬件逻辑。普通 GET 成功读取失败操作仍返回退出码 0；submit --wait 得到 failed/unknown 等非成功终局时为 9。HTTP 接受不等于总线事务成功。

## 临时上下文与互斥

一次有限事务不要求调用者先建立 UART 会话。服务原子取得兼容资源，创建自己拥有的临时调试上下文；节点确认就绪后才发布总线命令。不会借用现有用户会话，也不会自动关闭其他人的上下文。

旧固件在调试上下文内同时启用 UART，探针板还关联 UART2 输入。资源计划明确包含这些关联资源，不能将当前执行称为完全隔离的 I²C 驱动。这个内部上下文不会加入受管 UART 租约表或创建 RX 流，也不能通过 UART 续租/写入接口操作。

操作响应的 session_id 在此动作中是内部诊断上下文身份，不是调用者取得的串口会话。应通过操作查询和取消管理它，不能用 session show 将其视为用户 UART 控制权。

两块已支持的板在旧台架上共用物理线，当前适配器保留全局保守互斥。不同 physical_bus_id 不解除这条适配器限制。新操作准备中、执行中或清理未确认时，其他节点和旧 /api/i2c 入口都不能抢占。

已派发且结果未知的旧 I²C 记录若还关联未释放上下文，也会阻挡新操作。新受管事务的旧取消接口和旧上下文关闭接口返回冲突，必须通过操作账本取消，避免绕过结果与资源核对。

## 状态与清理

阶段为 accepted → dispatched → releasing → finished。accepted 可包含准备内部上下文；事件区分上下文发布、就绪与总线发布。

dispatch_within_ms 最多 10 秒，从接受起算并包含准备时间。execution_timeout_ms 最多 10 秒，在兼容路径中是服务完成确认窗口，不是传递到节点的新执行预算。旧主机驱动仍使用其内置有限调用机制。

总线终结报告、期限到期或取消都会停止继续下发并关闭内部上下文。清理最多再等待 10 秒作结果核对；无法确认时结束客户端等待，返回 unknown/unconfirmed，但继续保留适配器占用。它不是强制释放超时。

只有节点报告上下文结束、已核对启动变化，或确定上下文从未发布，才释放预留。没有管理员“忽略未知、立即抢占”的新接口。节点失联时需恢复可核对的状态；不能通过换请求键、连接别名或旧接口绕过占用。

## 结果与证据

整体 outcome 与 result.bus_outcome 分开：即使读回成功，若上下文释放未确认，整体仍可为 unknown，read_data_hex 及主机报告继续保存。释放确认后可在原操作上修订为最终总线结果，保留事件历史。

| 字段 | 含义 |
| --- | --- |
| code | I2C_MASTER_COMPLETED、I2C_NACK、I2C_TIMEOUT、I2C_BUS_NOT_IDLE 等机器原因 |
| requested_write_bytes/read_bytes | 规范请求中的字节数量 |
| confirmed_write_bytes/read_bytes | 可由主机完成报告支持的数量；未知为 null，不伪造部分进度 |
| observed_read_bytes | 实际报告附带的字节数量，与完整事务确认分开 |
| read_data_hex、read_complete | 保存原始报告数据并说明是否满足完整请求长度 |
| master_report | 原驱动报告，包括错误、耗时及可得的线路空闲观测 |
| bus_effect、effect | 总线作用与关联准备作用分开，准备可能已经启用 UART |
| cleanup_confirmed | 是否已核对内部上下文结束 |

NACK 可能发生在地址或数据阶段，旧驱动无法精确区分；不推断已经写入了多少字节。超时也可能已有副作用。线路非空闲报告且观测到低电平时，可以说明未启动事务；其他模糊错误不能包装成零副作用。

当前不提供可靠的中途字节进度，因此不编造 partial 计数或部分成功结果。对成功主机报告校验预期读长度，长度不符保留原数据并判失败。总线 ACK 或主机完成不证明外设业务状态正确，也不证明目标身份经过实测。

## 幂等、重启与取消

相同请求键先查持久账本，再检查资源，重试不会创建第二个上下文或总线事务。I²C 写入十六进制归一为小写，消息顺序保留，规范化标识为 strict-json-i2c/1。同键不同意图冲突，不用新键自动掩盖未知结果。

总线命令只发布一次，不因响应丢失、设备重报或服务重启再次发布。读取也可能清标志或弹出 FIFO，因此读取同样不自动重试。旧节点内存去重与服务账本不构成掉电后的物理恰好一次保证。

未发布总线命令时取消可以确认 bus_effect=none，但准备上下文可能已经启用关联接口。已发布后不能可靠撤回短事务，取消进入清理并保留不确定性；迟到的匹配完成证据优先于取消意图。

只接受匹配执行节点、启动和事务身份的首个终结报告。确认缺失造成的未知结果可被迟到报告修订；不重新执行原意图。旧节点在上下文结束后可能保留 I²C 报告，因此报告身份核对独立于当前 UART 会话名。

## 范围与验证

不支持扫描、十位地址、跨调用占用总线、任意消息序列、动态引脚或其他芯片。准备前仍要求节点新鲜、配置修订有效及所需固件能力匹配；未派发操作不离线排队。

成功单写、单读、写后读、各类故障与清理边界已通过模拟节点验证。当前实板仅验证无应答路径，尚未连接真实外设验证 ACK 与业务数据读回，见 [验证记录](../validation/2026-10-27-i2c-operations.md)。

迁移 0005 追加事务关联及适配器占用记录，原有数据保持。统一响应契约仍位于兼容旧路径 contracts/compat/uart-operation-v0/result.schema.json，已包含 I²C 变体及 releasing/release_pending。
