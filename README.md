# Firefly

远程嵌入式开发与调试系统。

面向人类、脚本与 AI 的多节点开发工作空间。节点主动连接控制服务，CLI、SDK 和网页共用操作、资源与证据。当前支持 ESP32-S3；后续节点通过相同协议和独立驱动接入。

## 开发入口

```powershell
git clone https://github.com/Bit-Walker/Firefly.git
cd Firefly
.\tools\bootstrap.ps1
.\tools\edbg.ps1 node list --credentials-file fleet/.state/admin.local.json --json
.\tools\edbg.ps1 capability list --node <节点ID> --credentials-file fleet/.state/admin.local.json --json
```

CLI 提供节点/能力查询、版本化目标与连接、无副作用计划、持久操作、串口接收流、会话租约和镜像目录。节点 OTA 要求新健康启动及运行 Flash 的完整文件 SHA-256；不存在版本字符串确认的降级路径。

## 当前能力与约束

- 节点协议 `edbg.agent/v1`：芯片家族、构建信息、运行镜像、健康状态和版本化驱动能力。
- 持久操作：接受、幂等、单次发布、取消边界、未知结果和后到证据核对。
- UART：打开/写入/关闭/显式续租，多读者 RX 日志及独立游标。
- I²C：单写、单读、写后读；当前驱动仍绑定固定引脚并使用临时调试上下文。
- 镜像与 OTA：结构、校验和与 SHA-256 验证；镜像登记、撤回、节点维护互斥和运行镜像核对。
- 原型驱动还提供有限波形采集与解码，尚待迁入统一执行模型。

UART/I²C/波形仍有固定台架、资源耦合和协议限制。尚未实现通用外部烧录、动态引脚、独立客户端授权和其他芯片驱动。当前服务为单管理员、自建部署，不将能力声明当作现场接线证明。

## 文档

- [当前工程状态](docs/current-state.md)
- [研发顺序与验收](docs/architecture/08-next-implementation-batch.md)
- [系统目标与对象](docs/architecture/README.md)
- [运行信息协议](docs/reference/agent-runtime.md)
- [镜像与节点更新](docs/reference/node-update-v0.md)
- [UART 操作](docs/reference/uart-operations-v0.md)、[数据流](docs/reference/uart-streams-v0.md)、[租约](docs/reference/uart-leases-v0.md)
- [I²C 操作](docs/reference/i2c-operations-v0.md)
- [配置与计划](docs/reference/workspace-v0.md)、[CLI](docs/reference/cli-query-v0.md)
- [开发指南](docs/guides/development.md)、[服务部署](fleet/DEPLOY.md)

初期开发直接调整设计和接口，不为尚未发布的历史版本增加兼容层。文档维护当前事实、接口和待办；验证原始数据保存在本机 artifacts/，不逐批追加开发流水账。
