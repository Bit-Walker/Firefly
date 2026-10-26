# 开发与运行

CLI/API 是默认开发和验收入口，UI 使用同一业务接口。工程处于未发布阶段，协议或模型不合适时直接调整，不增加历史兼容分支。

## 目录

| 路径 | 职责 |
| --- | --- |
| apps/control-plane/src/edbg_server | 服务、配置、计划、操作、存储与轮询协调 |
| apps/cli/src/edbg_cli | 参数、JSON 包络及机器退出码 |
| packages/sdk-python/src/edbg_sdk | HTTP 客户端，不读取服务数据库 |
| packages/analysis/src/edbg_analysis | 离线纯分析 |
| fleet/firmware/main | ESP32-S3 节点、驱动及运行镜像测量 |
| contracts | 请求、结果、数据流与运行信息契约 |
| hardware/boards | 板级约束 |
| tests | 契约、故障和集成验证 |
| fleet/deploy | 自建部署配置 |

fleet/server.py 等启动别名指向同一个服务；不要重复实现业务逻辑。默认私有状态为 fleet/.state，可由 FLEET_STATE 指定；镜像目录与服务数据库始终使用同一状态根。证书、管理员密码、设备令牌和数据库不进入公开源码包。

## 准备与运行

```powershell
.\tools\bootstrap.ps1
.\tools\edbg.ps1 health --json
.\fleet\tools\start.ps1
```

先确认没有重复服务，切换前核对活动操作和资源。控制台为 http://127.0.0.1:8088，设备 TLS 为 8443；停止使用 fleet/tools/stop.ps1。服务需要完整仓库布局及板级/契约文件，当前不是可独立复制的单个 Python 模块。

节点固件使用本机 ESP-IDF：

```powershell
.\fleet\tools\build.ps1 -Version 4.0.0
```

源码包含全部驱动，但运行能力以节点 runtime 报告及实际提供者为准。修改固件后必须做对应实板验证；不要把编译成功视为硬件验证。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m unittest discover -s fleet/tests -p 'test_*.py'
.\.venv\Scripts\python.exe contracts/tools/check_drafts.py
```

源码仓库不包含编译产物。真实固件镜像测试在缺少 `fleet/artifacts/4.0.0/fleet_agent.bin` 时会明确跳过；运行上述 4.0.0 固件构建命令后可执行该项验证。

这些测试使用隔离状态和模拟节点，不能证明真实外设行为。硬件验证显式选择操作与对象，保存原始结果；UI 未改时不重复运行浏览器测试。接收流是平铺的记录，RX 字节位于 item.data_hex。

当前规划、证据与限制见 [当前状态](../current-state.md)、[研发顺序](../architecture/08-next-implementation-batch.md)。

## 分发与恢复

```powershell
.\.venv\Scripts\python.exe fleet/tools/package.py
```

产物为 fleet/artifacts/fleet-source-and-deploy.zip，保留仓库布局、当前固件和符号文件。分发前检查私有路径排除及解压运行；Compose 配置通过不等于镜像已构建或公网部署完成。

数据库升级使用事务、在线备份、校验及外键检查。恢复快照仅用于恢复开发现场；不能把旧快照中缺失的发布记录当作重新执行物理动作的依据。先保存现状并核对已经发生的副作用，再恢复源文件或状态。
