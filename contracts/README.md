# 契约目录

当前可执行查询入口的兼容契约：[CLI 查询结果 v0](compat/fleet-v1/cli-query.schema.json)。

统一动作、资源、租约、节点链路和状态机仍按架构方案讨论，不把现有原型接口直接冻结成未来标准。
能力查询的 `reported` 字段是当前固件报告的名称列表，不是完整的有效能力或动作参数规范。

## 实验性设计

[操作请求草案](drafts/operation-v0/README.md)包含七种动作的候选请求 Schema、样例和离线检查。operation-plan 接口复用其形状做预检查；执行接口接受四种 UART 动作、有限 i2c.transfer 和严格 image_identity 节点更新的受限兼容语义，具体以各自参考为准，节点运行信息使用 edbg.agent/v1。正式动作、结果、事件、流和节点链路仍需逐项收敛。

[操作响应契约](compat/uart-operation-v0/result.schema.json)定义当前 UART、I²C 和节点更新服务响应、释放阶段及兼容等级，保留历史目录名，不包含 SDK 等待附加字段。节点更新的 session_id=null，不借用 UART 会话。

[镜像清单契约](compat/artifact-v0/manifest.schema.json)描述共用节点固件目录。镜像导入校验分段、校验和、附加摘要和精确文件长度；运行确认另核对节点测量，见 [节点更新参考](../docs/reference/node-update-v0.md)。

[UART RX 页面契约](compat/uart-stream-v0/read.schema.json)描述有限读取；NDJSON 跟随、过期游标与恢复约定见 [接收流参考](../docs/reference/uart-streams-v0.md)。查询错误包络可带可选 context，提供恢复游标。

[会话响应契约](compat/uart-session-v0/session.schema.json)区分控制器租约与节点报告；续租样例包含预期租约修订，不能将其视为设备已确认的 fencing 编号。
