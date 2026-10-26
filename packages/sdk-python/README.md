# Python HTTP SDK

SDK 不导入控制服务，不读取服务端数据库，不自动寻找管理员文件。认证由调用者显式提供；HTTP cookie、同源请求头、错误转换和查询在 SDK 中完成。

```python
import json
from pathlib import Path
from edbg_sdk import Client

credentials = json.loads(Path('/private/operator.json').read_text())
with Client('https://debug.example.org') as client:
    client.login(**credentials)
    for node in client.nodes():
        print(node['id'], node['online'])
```

查询方法：`health()`、`nodes()`、`node(id)`、`profiles()`、`capabilities(node_id)`、`jobs(node_id=None)`。`ClientError` 提供 `code`、`message` 和可选的 `status`；详情见 [CLI 参考](../../docs/reference/cli-query-v0.md)。

`get/post/upload/job/wait` 暂作现有运维脚本的兼容方法，其中 POST、上传和任务提交会改变系统状态。它们使用现有 API，并不代表未来统一动作契约。`wait` 超时不取消远端任务；进度由可选回调传出，SDK 不向标准输出写日志。

`nodes()` 返回兼容 API 的原始对象，CLI 另做公开字段投影。不要直接将原始对象当作未来稳定的节点规范。

版本化配置与预检查方法：`configurations(kind)`、`configuration(kind,id,revision=None)`、`apply_configuration(kind,configuration,expected_revision)`、`plan(request)`；kind 为 target 或 connection。具体边界见 [工作空间接口](../../docs/reference/workspace-v0.md)。

兼容执行接口：`submit_operation`、`operation`、`operations`、`operation_events`、`cancel_operation`、`wait_operation`。提交请求必须自带并保存幂等键；等待断网/到期返回原操作身份，不自动取消或创建新意图。详见 [UART 参考](../../docs/reference/uart-operations-v0.md)和 [I²C 参考](../../docs/reference/i2c-operations-v0.md)。I²C 的 releasing 阶段仍需等待；即使总线已完成，也要另看资源释放结果。

RX 方法：`streams`、`stream`、`read_stream`、`follow_stream`。每个消费者单独保存 next_after；过期错误包含显式恢复位置，跟随异常保留 resume_after。见 [接收流参考](../../docs/reference/uart-streams-v0.md)。

`session(id)` 与 `sessions(node=...,before=...,limit=...)` 提供受管上下文查询。续租使用带 expected_lease_revision 的 submit_operation 请求，不隐式产生新键或自动续租；见 [续租参考](../../docs/reference/uart-leases-v0.md)。

`artifacts(before=...,limit=...)`、`artifact(id)`、`import_artifact(path)` 提供共用镜像目录，导入核对摘要与长度回执。node.update 使用既有操作方法；其 session_id=null，确认级别固定为 image_identity，运行摘要与长度必须匹配，等待超时不重发。见 [节点更新参考](../../docs/reference/node-update-v0.md)。
