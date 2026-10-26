# 镜像目录与节点更新

node.update 是更新执行节点自身代理的有限操作，与外部 target.flash 分开。当前提供者支持 ESP32-S3，要求 edbg.agent/v1 以及 node.update/1、runtime.image/1。构建版本不作为能力开关。

## 镜像目录

```powershell
.\tools\edbg.ps1 artifact import --image fleet/artifacts/4.0.0/fleet_agent.bin --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 artifact list --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 artifact show <镜像ID> --credentials-file fleet/.state/admin.local.json --json
```

导入只登记字节。服务检查芯片、项目、完整分段边界、对齐校验和、附加 SHA-256 和精确长度；不接受被截断、有多余尾部或摘要错误的镜像。目前仅支持带附加摘要的规范未签名 ESP 应用，最大 3 MiB，不验证发布者签名或保证应用业务正确。

ID 为完整文件 SHA-256 的前 24 个十六进制字符，操作必须同时携带完整摘要；前缀冲突拒绝。相同字节返回同一记录，重新导入不解除撤回或覆盖损坏对象。文件原子提交，私有路径不公开。计划、派发和下载另行核对文件内容，未登记文件不能发布。

SDK 的 import_artifact 采用有界读取，核对服务回执中的 ID、摘要和长度。目录 API 为 GET/POST /api/workspace/artifacts、GET /api/workspace/artifacts/{id}；列表使用 before/limit 和 next_before。清单 Schema 位于 contracts/compat/artifact-v0/manifest.schema.json。

## 提交更新

先查询 maintenance/agent 连接及修订，保存一次意图及新幂等键：

```json
{
  "api_version": "edbg.operation.draft/0",
  "connection": {"id": "实际维护连接ID", "revision": 1},
  "action": "node.update",
  "params": {
    "image": {"artifact_id": "实际镜像ID", "sha256": "实际64位小写SHA256"},
    "confirmation": "image_identity"
  },
  "idempotency_key": "REPLACE_WITH_A_NEW_RANDOM_KEY",
  "timing": {"dispatch_within_ms": 10000, "execution_timeout_ms": 180000}
}
```

```powershell
.\tools\edbg.ps1 operation plan --file update.json --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation submit --file update.json --wait --wait-timeout 190 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation get <操作ID> --credentials-file fleet/.state/admin.local.json --json
```

唯一确认级别为 image_identity。boot_version 不再属于请求契约；/api/jobs 也不能创建 OTA。没有弱确认回退或并列的旧更新模式。

接受时重新检查配置、在线状态、健康、启动、驱动、镜像和资源，冻结解析后的绑定，原子创建一个操作、一个节点任务及维护占用。session_id=null，不制造串口会话。派发期限最多 30 秒，服务完成确认窗口最多 300 秒，尚未向节点传递新的逐动作截止预算。

## 成功证据

必须同时满足：关联任务报告成功、启动身份改变、目标版本匹配、runtime.health=valid，以及运行 Flash 的完整摘要、长度和项目与冻结镜像一致。仅新心跳、相同版本或下载完成不足以判成功。

结果 code=UPDATE_IMAGE_IDENTITY_CONFIRMED、runtime_image_identity_verified=true、verification=node_flash_sha256，并保存 observed_runtime。健康新启动报告了不同字节时，结果为 UPDATE_IMAGE_MISMATCH，不能用版本字符串覆盖。缺失运行证据或仍为 pending 时不确认成功。

测量原理与信任边界见 [运行信息协议](agent-runtime.md)。这是认证节点的 Flash 测量报告，不是硬件根远程证明。

## 交付、取消和未知状态

- 发布标记、任务与操作事件同事务提交。命令只发布一次，不自动重发；原键重试查询原意图。
- 更新占有节点维护资源。已有活动任务或未释放调试上下文会阻挡接受；新旧调试及任务入口不能抢占维护占用。
- 未派发前取消、过期、撤回、镜像损坏或启动/能力变化均阻止下发。已派发 OTA 不能可靠中断，取消返回 CANCELLATION_UNSUPPORTED。
- 无终结证明时返回 unknown/unconfirmed 并继续持有维护资源。断网、撤销凭证和客户端等待超时均不证明任务停止，不能靠新键抢占或补发。
- 迟到有效证明修订原结果。首个有效终结报告保留，后续矛盾报告不覆盖它；无关任务 ID 不确认。

当前只有逐节点、单管理员操作，没有灰度发布、自动恢复、强制维护解锁或外部目标烧录。这些功能需要各自的真实提供者与证据。
