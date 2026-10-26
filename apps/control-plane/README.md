# 控制服务

一个应用、一个 SQLite 数据库和私有镜像目录。

- runtime.py：版本化节点事实、健康、运行镜像与驱动能力。
- workspace.py / planning.py：目标、连接、配置修订、快照及资源预检查。
- operations.py：持久意图、幂等、事件、取消及证据核对。
- node_updates.py / artifacts.py：严格运行镜像确认、节点维护和内容寻址固件。
- streams.py：受管 RX 日志、独立游标、保留和缺失。
- api/：当前 UART、波形和 I²C 驱动适配；固定台架部分待拆分。
- migrations.py：事务升级、私有在线备份与结构完整性。

SDK 只访问 HTTP；设备主动轮询，只有轮询响应发布命令。真实副作用与服务记录的提交并非分布式原子事务，不声称物理恰好一次执行。

[开发指南](../../docs/guides/development.md) · [研发顺序](../../docs/architecture/08-next-implementation-batch.md)
