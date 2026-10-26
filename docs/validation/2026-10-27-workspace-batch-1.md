# 第一批工程整理验证

日期：2026-10-27。范围：源码归属、兼容入口、只读机器客户端、部署路径与分发包。

## 交付范围

- 控制服务迁入 `apps/control-plane/src/edbg_server/`，网页迁入 `apps/web/`。
- 建立 Python HTTP SDK 和只读 CLI，查询健康、节点、能力、接口描述与任务。
- 波形纯统计进入 `packages/analysis/`；当前板级接口描述进入 `hardware/boards/`。
- 旧 `fleet/` 导入入口保持同一模块身份；原启动、证书、私有数据库和固件位置保留。
- 容器与源码分发采用新目录，补齐开发指南、CLI 参考及 ADR 0002。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 原服务端测试 | 44 项通过，使用临时数据库；覆盖认证、任务、会话、I²C 和波形等原语义 |
| 新机器客户端测试 | 6 项通过；覆盖显式认证、字段投影、只读查询、节点能力范围、错误和无效输入 |
| 原客户端数据模型测试 | 30 项通过，仅更新模块路径，无浏览器流程 |
| 模块兼容性 | 四个旧模块均与正式模块为同一对象，无重复应用或状态 |
| 静态资源迁移 | 所有原网页源文件哈希一致；11 个页面引用的 JS/CSS 资源返回成功 |
| 本地服务 | 原 8088/8443 入口恢复，两节点持续在线，仍运行 3.5.0 |
| 只读接口 | 健康、节点、能力、接口描述和任务查询正常 |
| Compose | 新构建上下文配置检查通过 |
| 源码分发 | ZIP 完整性及白名单路径检查通过；从解压目录验证服务导入、页面与接口描述 |

CLI 的错误测试包括未认证、连接失败、无效 JSON、非有限 JSON 数字、错误结构、错误成员、非法参数和 URL 中嵌入凭证。不会把认证内容或原始错误响应写入机器错误结果。

服务切换前确认活动任务为零，等待原调试会话自然结束；保存 SQLite 在线备份后切换。没有提交 OTA、重启、串口发送或 I²C/波形动作。

本机 Docker 引擎未运行，本批镜像构建尚未验证；公网部署、云端证书与跨互联网实测仍待服务器操作。此次验证不代表新动作协议、多型号或完整开发闭环已实现。

## 证据位置

以下为本机生成的证据，不随公开源码包分发：

- `artifacts/structure-bootstrap.log`：项目环境安装。
- `artifacts/structure-server-tests.log`：原服务端测试。
- `artifacts/structure-machine-tests.log`：新机器客户端测试。
- `artifacts/structure-client-model-tests.log`：客户端数据模型测试。
- `artifacts/structure-web-hashes.json`：切换前网页源文件哈希。
- `artifacts/structure-live-check.json`：只读联机检查摘要。
- `artifacts/structure-package-check.json`：源码分发检查摘要。

迁移前公开源文件快照：`artifacts/structure-batch-1-before.zip`。私有数据库备份：`fleet/.state/pre-structure-batch-1.local.bak`。分发包及 SHA-256：`fleet/artifacts/fleet-source-and-deploy.zip` 和同名 `.sha256` 文件。

## 后续工作

本批是工程边界整理和查询入口，并未完成 M1。接下来应先收敛 UART、I²C、烧录三个场景的最小动作契约，再实现：

1. 节点与目标、连接配置及有效能力模型，去除服务与固件中的固定台架身份依赖。
2. 统一操作账本、资源与租约语义、幂等及未知结果处理。
3. 面向 CLI/AI 的动作、事件、流和产物访问，以及独立身份与授权。
4. 再迁移 ESP32 固件执行器，并由实际用例推动网关与其他芯片适配。
