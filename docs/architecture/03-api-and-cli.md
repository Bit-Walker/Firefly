# 统一接口与命令设计

状态：讨论提案 v0.3；语法、字段、命令名和版本号未冻结。更新日期：2026-10-27。

本轮具体候选规则见 [三场景推演](06-scenario-walkthroughs.md)及[操作契约草案](07-operation-contract-draft.md)。下面为总览；离线 Schema 与样例位于 `contracts/drafts/operation-v0/`，均不是当前线上接口。

## 客户端边界

CLI、SDK、网页和后续 AI 适配调用同一套查询、操作、会话、数据流与产物接口。权限、资源、幂等和结果判断属于服务端及节点共同执行的规则，不分别写进各客户端。

MCP 是可选适配层，不作为核心协议的唯一入口。北向接口建议先采用 HTTP 与 JSON；连续事件可以提供流式输出，二进制数据和大文件采用独立传输。节点链路及编码保持可适配。

能力和动作契约需要版本。服务端规划、客户端类型和节点执行应来自同一份逻辑契约，节点仅实现并接受其支持的动作子集。

## 公共动作及扩展

初步命名包括 `uart.session.open`、`uart.session.close`、`uart.write`、`i2c.transfer`、`spi.transfer`、`gpio.read`、`gpio.write`、`logic.capture`、`target.flash`、`target.reset`、`node.update`。

动作名称稳定不代表所有设备都支持它。动作入口必须结合能力及连接配置验证。型号特有动作使用扩展命名空间，避免破坏公共动作语义。

本轮建议 UART 接收采用数据流查询，多个消费者独立持有游标；便捷读取命令解析到流查询，不创建竞争消费缓冲的 `uart.read` 硬件动作。健康、能力等纯查询同样不强制纳入操作账本。

## CLI 形式

以下使用暂定命令名 `edbg`，以下动作、连接与流命令为设计示例；已经实现的 node/capability 查询以 [CLI 参考](../reference/cli-query-v0.md) 为准。

```sh
edbg node list --json
edbg capability list --node probe-01 --json
edbg connection show sensor-bus --json

edbg stream read stream-123 --after cursor-123 --limit-bytes 4096 --json
edbg i2c transfer --connection sensor-bus --address 0x76 --write D0 --read 1 --json

edbg operation submit --file request.json --wait --wait-timeout 30s --json
edbg operation get op-123 --json
edbg operation watch op-123 --format ndjson
edbg operation cancel op-123
```

便捷命令负责表达常用意图，通用入口负责提交完整规范请求。两者必须得到相同的动作模型及服务端执行路径。

## 机器输出约定

- 人类终端可以默认显示表格；机器显式选择结构化输出。
- 标准输出仅放结果或事件，进度和诊断放标准错误。
- 普通结果采用 JSON；连续事件采用 NDJSON 等明确的流格式；二进制产物不混入 JSON 日志。
- 错误包含稳定错误码、说明、相关资源及是否可安全重试的依据。
- 支持文件和标准输入，便于脚本与 AI 组合调用。
- CLI 可接受 `3s` 这样的单位，规范请求使用明确单位及范围的字段。
- 等待超时或执行状态未知时仍返回原操作身份，客户端可以继续查询。
- 输出格式、退出码、分页、游标与截断说明作为公开兼容性契约。

## 请求示意

请求候选结构如下，字段与语义以第 07 章及草案 Schema 为准：

```json
{
  "api_version": "edbg.operation.draft/0",
  "connection": {"id": "sensor-bus", "revision": 1},
  "action": "i2c.transfer",
  "params": {
    "address": 118,
    "speed_hz": 10000,
    "messages": [{"write_hex": "d0"}, {"read_length": 1}]
  },
  "idempotency_key": "example-intent-i2c-read",
  "timing": {"dispatch_within_ms": 5000, "execution_timeout_ms": 1000}
}
```

每个动作的参数边界由能力进一步收紧。服务端接受时冻结实际绑定、启动上下文、配置与能力修订；预期连接修订是候选必填项。客户端等待预算单独处理，不混同派发期限或节点执行预算。

## 能力发现与预检查

客户端先查询能力与有效配置，再构造请求。预检查可返回参数问题、资源需求、预计占用和权限要求；如果未预约资源，预检查不能保证稍后执行一定可用。

节点在实际执行前进行最终验证。客户端已有描述或服务端先前检查成功，不构成跳过本地校验的理由。

## 下一轮待定

1. 操作、会话、订阅和租约的创建及引用方式。
2. 动作参数结构、编码、长度限制和时间单位。
3. 状态、结果、验证证据、错误码及退出码。
4. 事件顺序、消费者游标、背压和产物获取方式。
5. 幂等键作用域、有效期、规范请求比较与断联核对。
6. 契约定义格式、生成工具及节点链路的版本协商。
