# UART 多消费者接收流

状态：已实现 v0，2026-10-27。受管 UART 会话提供持久、有界的 RX 记录流；CLI 与 SDK 可用独立游标读取同一数据。网页未改版，可通过相同 HTTP 接口接入。

## 找到数据流

新 UART 打开操作成功时，result.stream_id 给出 RX 流身份；也可按 session_id 查找：

```powershell
.\tools\edbg.ps1 stream list --session <会话ID> --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 stream show <流ID> --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 stream read <流ID> --limit 100 --limit-bytes 4096 --credentials-file fleet/.state/admin.local.json --json
```

stream list 还支持 --node、--before、--limit。当前每个受管会话有一条 RX 流，包含其实际启用的通道。普通节点为 UART1；旧探针上下文还包含 UART2 监听。TX 意图继续通过操作账本查询，尚未合并成双向时间线。

流绑定固定的节点、启动标识、会话和连接快照。新会话或新启动不能复用旧流身份。旧浏览器会话不自动成为新流；迁移前已经存在的受管会话会回填保留下来的旧记录，并明确标记历史不完整。

## 有限读取与恢复

有限读取仍使用 edbg.cli.query/v0 包络，data 为 edbg.stream.read/v0 页面。页面含 items、next_after、has_more、eof、payload_bytes、scanned_records，以及流元数据和保留范围。

每个读者保存自己的 next_after，后续使用 --after。读取不消耗数据、不修改其他读者位置、不确认设备缓冲、不创建会话，也不续租。

```powershell
.\tools\edbg.ps1 stream read <流ID> --after <上次next_after> --port 1 --credentials-file fleet/.state/admin.local.json --json
```

不提供 --after 时，从最早保留记录开始，并明确返回 history_truncated。提供的游标早于保留范围时，HTTP 410 / CURSOR_EXPIRED，CLI 退出码 10，error.context 返回 earliest_after、latest_after 和 trimmed_through。调用者显式选择从最早保留位置继续，或跳到最新位置等待新数据；工具不自动选择和跳过。

游标只适用于它所属的流；跨流、未来位置或格式错误返回 CURSOR_INVALID。游标不是认证凭证，所有读取仍要求管理员身份。

--limit 约束扫描记录数，范围 1–200；--limit-bytes 约束返回的原始 RX 字节数，范围 128–65536，默认 4096。数据返回十六进制，不向终端直接输出控制字符或假设文本编码。

--port 1/2 只筛选 RX 数据；全流缺失与结束记录仍会返回，因为旧节点的缺失无法精确归属到某个端口。跳过其他端口的记录也会推进 next_after，可能得到空 items 但 has_more=true；这不表示端口静默。

## 跟随输出

```powershell
.\tools\edbg.ps1 stream follow <流ID> --port 1 --duration 30 --interval 0.5 --credentials-file fleet/.state/admin.local.json
```

follow 使用有限 HTTP 分页轮询，并向标准输出写 NDJSON；可显式写 --format ndjson。每行 api_version 为 edbg.stream.follow/v0：

| kind | 内容 |
| --- | --- |
| page | data 为完整读取页面，含 next_after |
| checkpoint | resume_after 与停止原因 duration/source_ended/interrupted |
| error | 结构化错误、最后游标及可能的恢复位置 |

默认跟随 30 秒，duration 必须为正且不超过 86400 秒。发生新数据、游标推进或结束时输出页面，不持续打印重复空页。到时或源结束返回检查点；Ctrl+C 返回检查点并退出 130。断网返回最后已交付游标和非零码，不自动重开串口或重新发送命令。

follow 总是 NDJSON，包括解析出该子命令后的认证、参数或读取错误；不会再追加普通查询包络。--json 不会将它变成一个无限 JSON 数组。输入命令本身无法解析时，仍沿用通用 CLI 错误处理。

## 记录与证据

记录 offset 是流内递增位置，source_seq 是旧节点的会话内序号；二者不同。节点序号跨 UART1/UART2 共用，服务端按有效快照排序、保存并去重。

| kind | 语义 |
| --- | --- |
| rx | 原始字节、port、source_seq、source_time_us、received_at |
| source_gap | 已观察到的节点序号缺口，只知道缺失块范围，不知道字节数或端口 |
| source_counters | 节点丢块与 UART 错误的累计值及可得增量 |
| source_discarded | 收到未配置端口的块，记录原因，不冒充有效 RX |
| history_unavailable | 回填时无法恢复的历史或前缀 |
| source_ended | 当前来源结束及尾部完整性说明 |

丢块计数与序号缺口可能描述同一次损失，不能直接相加。UART 错误计数也不是损坏字节数。source_time_us 是节点从驱动读取块时的启动后时间，不是逐字节线缆到达时间；received_at 是服务接收时间，不能据此承诺跨节点精确同步。

设备 ACK 由服务端的持久检查点产生。数据、缺失记录、检查点与轮询响应在同一 SQLite 事务中处理；事务失败不会提交确认，设备重报不会产生第二份已提交记录。读者游标永远不参与 ACK。

origin_at_session_start 表示日志在受管会话创建时建立，不保证所有线缆字节都被捕获。retained_from_origin 表示服务保留策略尚未裁剪日志开头。迁移回填的 origin 为 legacy_backfill，历史计数也可能不完整，不将它包装成从开机起的完整采集。

旧固件缺少关闭前全量排空/最终序号确认。尽可能保存关闭或过期快照内的数据后，tail_complete 仍固定为 false。eof 只说明服务已记录来源结束且当前页到达保留日志末尾，不证明尾部字节无遗漏。离线或服务端租期到期本身不会被伪造为已确认的流结束。

确定从未发布到节点的打开意图被取消或到期时，也可结束其空流。新追加结束记录的 end_evidence 区分 server_never_published、node_session_end、node_boot_changed 和 legacy_state；空流结束不是节点已配置或释放硬件的证据。

## 保留策略与容量边界

新流存储当前采用固定默认值，并通过元数据 limits 返回：每流最多 2048 条记录/256 KiB 原始载荷，全局最多 65536 条记录/8 MiB 原始载荷。裁剪从最旧记录开始，保留单调游标下界与源检查点；慢读者会明确收到过期错误。

这些限额不包含 JSON、索引、流元数据、操作账本、备份、SQLite 空闲页或原 debug_rx 副本，不是数据库文件大小上限。当前未提供自动会话历史清理或管理员修改配额 API。

元数据与流读取使用一致的只读数据库快照，不会为了读取而刷新操作超时或执行清理。source_checkpoint 在数据按策略裁剪后仍保留，避免向设备回退 ACK。

## API 与 SDK

| 方法与路径 | 用途 |
| --- | --- |
| GET /api/workspace/streams?node=ID&session=ID&before=ID&limit=N | 流列表 |
| GET /api/workspace/streams/{id} | 来源、绑定与保留范围 |
| GET /api/workspace/streams/{id}/read?after=CURSOR&limit=N&limit_bytes=N&port=N | 有界读取 |

SDK：streams、stream、read_stream、follow_stream。follow_stream 为页面迭代器，异常的 context.resume_after 保留最后交付游标。当前认证仍是单管理员，尚未增加独立观察者身份或权限。[续租](uart-leases-v0.md)必须单独显式提交，读取与跟随不隐式续租。

迁移 0003 追加流、记录与预算表，原始数据不删除。旧受管会话回填在独立原子导入中执行一次。读取契约位于 contracts/compat/uart-stream-v0/read.schema.json；新协议的节点端排空确认、持久采集与统一双向流仍待后续开发。
