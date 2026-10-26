# UART 接收流验证

日期：2026-10-27。此为接收流阶段记录，后续显式续租见 [验证记录](2026-10-27-uart-leases.md)。范围：持久 RX 流、独立读者、游标、缺失记录、保留策略、CLI 有限读取与 NDJSON 跟随。参考：[使用说明](../reference/uart-streams-v0.md)。

## 软件验证

52 项机器客户端/集成测试通过，其中本批新增 14 项；44 项原服务端回归通过。主要覆盖：

- 同一页面由两个读者独立读取，读取前后完整数据库 SQL 快照一致。
- 轮询事务中断时不提交接收数据或 ACK 检查点，重报后只保存一份。
- 批内序号排序、跨报告去重、源序号缺口与设备计数分别记录。
- 每流和全局裁剪准确维护记录/载荷预算；游标过期后仅提供显式恢复位置。
- 端口过滤推进游标，字节预算不跳过尚未返回的数据。
- 源结束、节点换启动、最后一包过期快照以及旧记录回填。
- CLI 有限读取、跟随检查点与过期错误；SDK 跟随断网保留最后交付游标。
- 有限读取响应按公开 Schema 校验，读取既不消费记录，也不改变设备租期。

证据：本机 `artifacts/stream-tests.log`、`artifacts/stream-regression-tests.log`。保留策略和网络中断为隔离测试，不宣称在实板上制造了丢包、缓冲溢出或磁盘满。

## 实板验证

两板保持固件 3.5.0。通过新 UART 操作打开两端，使用已确认的 A4→B5 接线发送 20 字节随机标记。

两次独立 SDK 读取与 CLI 的 8 秒 NDJSON 跟随均获得相同完整字节，标记各出现一次。跟随输出提供可继续使用的 resume_after；读取前后 receiver 的 session_expires 完全一致。

测试后通过操作接口关闭两端，会话均 succeeded/released，接收流出现结束记录。eof 与 source_ended 成立，但 tail_complete 仍为 false；本次观测到完整标记不等于证明旧固件能保证任意关闭时刻的全部尾部字节。

证据：`artifacts/uart-streams-live-check.json`、`artifacts/uart-streams-follow.ndjson`。接收流为 `uart-rx-ae02c6c5dd153ca5a3ba33a3`。

## 数据与部署

迁移 0003 前，旧节点、固件、任务、调试、操作和配置记录数量已保存，迁移后数量一致。回填两条旧受管 RX 流，并标明 legacy_backfill；实板验证新增两条 live 流。

迁移私有备份：`fleet/.state/pre-uart-streams.local.bak` 和 `migration-0003-*.local.bak`。上版公开源码：`artifacts/pre-uart-streams-source.zip`。分发包检查结果：`artifacts/uart-streams-package-check.json`。

本轮未升级或重启开发板，未执行 I²C/波形，也未改网页或重复浏览器 UI 测试。Docker 构建与公网部署仍未验证。

## 限制

只增加受管会话 RX 流；没有统一 TX/RX 时间线、独立读者权限或续租 API。旧固件不能提供确定的最终排空证据，node source_seq 仍跨两个 UART 通道共用。

新流的记录/原始载荷限额不约束整个 SQLite 文件，不覆盖旧 debug_rx 副本、元数据、账本或备份。后续仍需统一数据生命周期与代理采集协议。
