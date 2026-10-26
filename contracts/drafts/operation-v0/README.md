# 操作请求草案 0

状态：实验性评审材料，2026-10-27。此目录保存候选请求形状。当前 operation-plan 接口复用 Schema 做无副作用预检查，样例身份需替换；执行接口接受 open/write/close/renew、有限 i2c.transfer 和严格 image_identity 节点更新的兼容子集，见 [UART 参考](../../../docs/reference/uart-operations-v0.md)、[I²C 参考](../../../docs/reference/i2c-operations-v0.md)及 [节点更新参考](../../../docs/reference/node-update-v0.md)。target.flash 尚不能提交执行；node-update 要求 image_identity，不支持版本字符串降级。

语义依据：[场景推演](../../../docs/architecture/06-scenario-walkthroughs.md)及[操作契约草案](../../../docs/architecture/07-operation-contract-draft.md)。

- `request.schema.json`：请求形状和动作专属参数约束。
- `fixtures/`：UART 打开、写入、关闭、续租、I²C 组合事务、外部目标烧录、节点更新的七个合规样例。身份和镜像摘要为占位值。
- `../../tools/check_drafts.py`：离线自检，检查 Schema、合规样例及关键拒绝情况，不连接服务或硬件。

```powershell
.\.venv\Scripts\python.exe -m pip install -r contracts/requirements-dev.txt
.\.venv\Scripts\python.exe contracts/tools/check_drafts.py
```

校验器还拒绝重复 JSON 键、非有限数值和浮点数。JSON Schema 对数学上为整数的浮点表示可能视为 integer，本草案另以严格 JSON 数值检查明确拒绝。

I²C 草案仅支持七位普通地址；一个请求的消息属于同一个目标地址，消息间重复 START、末尾 STOP。扫描、跨调用持有总线与十位地址暂不进入该子集。UART 停止位用字符串表达，以免混入浮点数。载荷、消息数量和时间的上限是讨论中的协议预算，不是某型号的能力承诺；有效能力可进一步收紧。

Schema 不验证身份存在、会话与连接匹配、镜像兼容性、权限、在线状态、资源、时钟和执行证据。完整结果、事件、流与节点链路契约仍需后续设计。离线工具可单独安装开发依赖；当前控制服务预检查也使用 jsonschema 与此请求 Schema 作为运行依赖。
