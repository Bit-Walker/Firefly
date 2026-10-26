# 镜像目录与节点更新验证

日期：2026-10-27。交付：镜像 HTTP/SDK/CLI 目录、受管 node.update 的显式 boot_version 路径、迁移 0006 与维护占用。接口见 [参考](../reference/node-update-v0.md)。

## 软件与迁移

103 项机器客户端/集成测试通过，比上一批新增 22 项；44 项旧功能回归通过。新覆盖：

- 内容身份、分页、撤回不被重复导入解除，非法/超限文件拒绝。
- SDK 对上传回执与有界文件输入的核对；新旧镜像目录共用同一隔离存储路径。
- 只读预检查、严格 image_identity 拒绝、没有虚假 UART 上下文。
- 新启动、精确版本和任务身份；矛盾终结报告不覆盖首个证明。
- 重试、不同意图同键冲突、并发占用、接受事务故障回滚。
- 丢失响应、独立进程恢复及长时间无确认不重发。
- 超时仍持有维护占用；后到进度不重开等待，后到终结证据修订原结果。
- 派发前取消、派发后不支持取消、旧取消/任务/调试入口保护。
- 撤回、文件损坏/丢失、启动改变和过期均在发布前拦截。
- 下载授权及文件完整性；撤销凭证不冒充已发布任务的停止证明。
- 有数据的 v5 迁移保留子表外键、索引和内容；重建之后失败全部回滚。

日志：本机 artifacts/node-update-tests.log、artifacts/node-update-regression-tests.log。原先 81 项也全部通过。没有重复浏览器 UI 测试。

在真实数据库的私有副本上先演练迁移，再停止空闲双端口服务并正式升级。迁移前后的历史计数保持一致：设备 2、任务 26、固件 10、调试上下文 53、操作 17、操作事件 59、I²C 绑定 1、接收流 6、接收记录 11。外键检查无错误，迁移版本为 1—6。

测试中发现导入模块顺序可能导致镜像目录与应用私有状态路径不同，已改为应用明确绑定，并新增路径一致性回归。17 个未登记的模拟小文件已从运行镜像文件夹隔离到本机 artifacts/node-update-test-quarantine/，正式固件目录记录和既有镜像未被替换。

## 实板闭环

使用 B 板 s3-fc012cca2cf8，将已部署的稳定 3.5.0 应用通过新 CLI 重新安装，没有编译或发布新固件版本。镜像大小 1,100,864 字节，完整 SHA-256：

```text
e9f0bb79e84d59efc513ef08d72625e8d9d79e93c133f65e6360669e68cea5cb
```

- 操作 d0e798ef2cc5e2701bd4cb7c，旧任务 19c9c851e644ceb533888a6e。
- 原启动 ff20d77ad02ce24b，新启动 9b58960be05c67b9；分区 app1 → app0。
- 节点报告 New firmware booted and passed online health check。
- 整体 succeeded/released，UPDATE_BOOT_VERSION_CONFIRMED，CLI 等待退出码 0。
- 原键重试返回原操作，只有一条关联任务和一次 update.command_published 事件。
- 两节点在线，A 的启动未改变，无活动任务、无活动调试上下文、无维护占用。
- 正式镜像记录仍 10 条，调试上下文仍 53 条；只新增一个操作和一个任务及相应事件。
- 控制服务再次重启后原键仍返回原终局；没有新增任务或操作、没有再次重启节点，发布事件仍只有一次。

证据：artifacts/live-node-update-request.json（提交前保存）、artifacts/node-update-live-cli.json、artifacts/node-update-live-check.json。它们不包含管理员密码或设备令牌。

实板结果只证明兼容级别 boot_version。runtime_image_identity_verified=false；本轮没有增加运行镜像摘要报告，未将同版本重装当作严格身份校验，也没有在实板制造丢电、回滚或故障镜像。

## 分发与恢复

分发仍使用 fleet/artifacts/fleet-source-and-deploy.zip；清单、私有路径排除和解压后测试见本机 artifacts/node-update-package-check.json。Compose 配置校验与镜像构建分别记录，未验证公网部署。

私有备份为 fleet/.state/pre-node-update.local.bak 和自动 migration-0006-*.local.bak；公开旧源码快照为 artifacts/pre-node-update-source.zip。不能删除迁移历史强制降级；恢复前保留当前账本并核对已经发布的 OTA，旧备份缺少的发布记录不能当作再次执行依据。
