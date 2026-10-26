# 连接注册表与操作预检查

状态：已实现 v0，2026-10-27。本页描述配置与兼容能力评估；已增加 [受管 UART](uart-operations-v0.md)、[有限 I²C](i2c-operations-v0.md)及 [兼容节点更新](node-update-v0.md)。外部烧录草案仍仅支持预检查。

## 命令入口

```powershell
.\tools\edbg.ps1 target list --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 connection list --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 connection show <连接ID> --revision 1 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 operation plan --file request.json --credentials-file fleet/.state/admin.local.json --json
```

`--file -` 从标准输入读取。CLI 仍使用 `edbg.cli.query/v0` 结果包络。预检查返回 blocked/unsupported/unknown 时，CLI 退出码仍为 0，表示成功完成评估；自动化必须检查 `data.status`。HTTP、认证或输入错误使用非零码。

配置写入是独立命令，不发送硬件指令：

```powershell
.\tools\edbg.ps1 target apply --file profiles/examples/workspace-target.json --expect-revision 0 --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 connection apply --file profiles/examples/workspace-connection.json --expect-revision 0 --credentials-file fleet/.state/admin.local.json --json
```

先替换连接示例中的节点 ID。`--expect-revision 0` 仅创建不存在的身份；更新必须显式给出当前修订号。冲突返回 `REVISION_CONFLICT`，CLI 退出码 7，不自动获取新修订重试。配置文件仅包含声明，不附带响应中的 revision/source/created 字段。

## 配置模型

目标声明包含 `api_version: edbg.target/v0`、`id`、`name`、`kind`。`kind=self` 必须指定已登记的 `node_id`；`kind=external` 不得指定节点身份。自有目标身份与执行节点身份保持显式关联。

连接声明包含：

| 字段 | 含义 |
| --- | --- |
| `api_version` | `edbg.connection/v0` |
| `id`、`name` | 稳定身份与显示名称 |
| `node_id` | 已登记且未撤销的执行节点 |
| `target.id/revision` | 固定目标修订，目标改名或更新不会改变既有连接 |
| `interface.id/kind` | 接口身份及 uart/i2c/maintenance 类别 |
| `adapter_id` | 所用适配器；未知适配器可以登记，但不能因此取得执行能力 |
| `physical_bus_id` | I²C 必填，其他接口不允许；同一实际总线应共用身份 |
| `enabled` | 默认 true；设置 false 创建新修订，阻挡该连接的后续预检查 |

维护接口必须引用执行节点自身的目标。所有身份最多 96 字符，允许字母、数字、点、下划线、冒号和连字符，首字符必须是字母或数字。类型严格，未知字段拒绝。

修订追加保存，不能原地修改；GET 可读历史修订。预检查要求连接当前修订，历史连接只供审计。目标修订固定在连接中；更换目标配置需显式更新连接。当前没有删除或历史清理 API，也没有通用资源调度器。

## HTTP 与 SDK

HTTP 继续采用本机现有管理员认证与同源检查，尚未提供独立 AI 身份。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/workspace/targets` | 当前目标列表 |
| `GET /api/workspace/targets/{id}?revision=N` | 当前或历史目标 |
| `PUT /api/workspace/targets/{id}` | 带 expected_revision 和 configuration 的追加更新 |
| `GET /api/workspace/connections` | 当前连接列表 |
| `GET /api/workspace/connections/{id}?revision=N` | 当前或历史连接 |
| `PUT /api/workspace/connections/{id}` | 带修订检查的追加更新 |
| `POST /api/workspace/operation-plan` | 接受操作草案请求，返回只读评估 |

SDK 对应 `configurations(kind)`、`configuration(kind,id,revision=None)`、`apply_configuration(kind,document,expected_revision)` 和 `plan(request)`；kind 为 target 或 connection。结构、权限与引用错误在 API 校验，CLI 不访问数据库。

## 预检查结果

输入采用 `edbg.operation.draft/0` 请求，见 `contracts/drafts/operation-v0/`。当前预检查接受七种草案形状，执行接口接受 UART 打开/写入/关闭/续租、有限 I²C 和严格 image_identity 节点更新的兼容子集。样例中的连接 ID、修订、会话或镜像必须替换成对应环境的实际值。

输出 `api_version=edbg.operation.plan/v0`：

- `status`：supported、unsupported、blocked 或 unknown。
- `reasons`：带 category/code/message 的机器原因，允许同时出现不同类别；汇总优先顺序为 unsupported、blocked、unknown。
- `binding`：连接与目标修订、节点启动、接口和适配器；已知适配器还带约束版本及报告摘要。
- `resources`：预计独占资源集合，不是已取得的锁。
- `facts`、`warnings`：区分配置声明、节点报告及未确认前提。
- `reserved=false`：没有预约；`submission_available` 表示当前是否有对应的兼容执行入口，受管 UART、有限 I²C 和显式兼容节点更新可为 true。实际接受仍重新校验资源与能力。

supported 表示此次快照通过当前兼容适配器检查，不保证目标存在、接线正确或稍后可执行。预检查不创建任务、不获取租约、不调用过期清理、不打开串口、不扫描总线，也不确认物理释放。

## 当前适配器实际范围

适配器 `esp32s3-gpio-v1` 要求 edbg.agent/v1 的健康运行信息和具体驱动能力。构建版本不作为开关；未知协议或家族不猜测支持。固定台架身份及引脚限制仍需按研发规划拆除，其他芯片驱动尚未实现。

UART 仅评估 uart1 的 115200/8N1、无流控及不超过 256 字节写入。旧会话没有连接修订快照，因此旧浏览器会话写入/关闭即使通过节点与权限检查，仍报告 `LEGACY_SESSION_BINDING_UNAVAILABLE`。旧控制权绑定登录 cookie，CLI 新登录不会自动继承浏览器会话。受管会话有独立持久绑定，按稳定管理员主体核对，见 UART 操作参考。

I²C 兼容评估及执行仅支持 i2c0 的 10 kHz、每方向最多 64 字节、单写/单读/写后读。保留当前适配器全局 I²C 互斥；使用者不能通过换 physical_bus_id 绕过它。旧调试上下文还会启用 UART，资源计划包含这些关联资源，不宣称已实现外设完全隔离。执行入口自行管理临时上下文，具体副作用与清理边界见 I²C 操作参考。

外部目标烧录返回不支持。node.update 要求 image_identity，计划核对节点协议、健康、镜像、摘要、撤回及本地文件内容。实际终结时必须取得新启动的运行 Flash 测量，不能凭计划判定更新成功；详见节点更新参考。

## 迁移与来源

迁移 0001 为原数据库追加配置表与编号校验账本。两进程启动通过 SQLite 写事务串行化迁移；首次应用前生成私有在线备份 `fleet/.state/migration-0001-*.local.bak`。迁移 SQL 与账本同一事务提交，失败回滚；源码校验不符或数据库版本更新时停止启动。

迁移后单独以原子事务导入当时已登记的旧台架元数据一次，source 为 legacy_import。每个旧节点获得 self 目标以及 uart-/i2c-/agent- 前缀连接；I²C 共用 legacy-i2c-bus，目标身份仅表示旧接线声明，未做外设识别。后续配置更新 source 为 operator_config。导入不修改固件、旧 BENCH 授权表或原接线。

新增实例需显式登记配置，首次导入后不会在每次启动时重新生成或覆盖。禁用连接只改变新配置接口的判断，不关闭旧会话，也不作为旧调试入口的访问撤销机制。
