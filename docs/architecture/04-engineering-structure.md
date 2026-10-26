# 工程结构与迁移建议

状态：单仓库组织方向已接受，第一批迁移已落地；完整目标结构仍分期实施。更新日期：2026-10-27。

## 仓库组织

采用单仓库，围绕契约、控制中心、客户端、节点代理和硬件描述组织。先保持一个控制服务、一个数据库及产物存储，不为目录中的模块分别部署微服务。

第一批已经建立 `apps/control-plane/`、`apps/cli/`、`apps/web/`、`packages/sdk-python/`、`packages/analysis/`、`contracts/compat/`、`hardware/boards/`、`profiles/examples/` 和 `tests/contract/`。旧 `fleet/` 保留兼容导入、工具、固件、状态和部署。

下方是完整目标结构，尚未出现的领域层、节点运行时、网关和其他芯片等目录仍为规划，不创建空工程。当前实际入口见 [开发指南](../guides/development.md)，迁移依据见 [ADR 0002](../decisions/0002-staged-workspace.md)。

```text
repository/
├─ README.md                         # 项目入口、运行入口和架构索引
├─ docs/
│  ├─ README.md                       # 全部文档入口
│  ├─ architecture/                   # 目标设计、工程结构和迁移路线
│  ├─ decisions/                      # 架构决策记录
│  ├─ reference/                      # 有实现依据的 API、CLI、协议参考
│  ├─ guides/                         # 部署、使用、开发和运维指南
│  └─ validation/                     # 验证方法、结果摘要及证据索引
├─ contracts/
│  ├─ compat/                         # 已有兼容查询包络
│  ├─ common/                         # 身份、单位、版本、错误等公共类型
│  ├─ actions/                        # 公共动作及命名空间扩展的契约
│  ├─ node-link/                      # 节点注册、控制、事件及版本协商
│  ├─ fixtures/                       # 不含凭证的规范请求与结果样例
│  └─ tools/                          # 契约验证与代码生成工具
├─ apps/
│  ├─ control-plane/
│  │  ├─ src/edbg_server/
│  │  │  ├─ api/                      # 公共接口适配与查询入口
│  │  │  ├─ application/              # 用例、操作调度、租约和数据服务
│  │  │  ├─ domain/                   # 对象、资源、执行语义与领域规则
│  │  │  └─ infrastructure/           # 数据库、传输、文件及外部进程适配
│  │  ├─ migrations/                 # 受版本管理的数据迁移
│  │  └─ tests/                      # 服务端模块级测试
│  ├─ cli/
│  │  ├─ src/edbg_cli/                # 命令、参数解析、输出及退出码
│  │  └─ tests/
│  └─ web/                            # 可视化客户端与波形等专用视图
├─ agents/
│  ├─ esp32/
│  │  ├─ main/                        # 启动和组件装配
│  │  ├─ components/
│  │  │  ├─ runtime/                  # 身份、链路、租约和本地执行上下文
│  │  │  └─ providers/                # UART、I²C、GPIO、采集、OTA 等执行器
│  │  └─ tests/
│  ├─ gateway/
│  │  ├─ src/edbg_gateway/
│  │  │  ├─ runtime/                  # 网关注册、任务和资源管理
│  │  │  └─ providers/                # USB、串口、烧录器、调试及构建工具
│  │  └─ tests/
│  └─ rp2/                            # 后续适配 Pico 系列时再建立
├─ packages/
│  ├─ sdk-python/                     # 客户端调用、事件订阅和产物访问
│  └─ analysis/                       # 协议、波形、日志及转储分析模块
├─ hardware/
│  ├─ boards/                         # 板型、引脚限制、接口与资源约束
│  └─ targets/                        # 目标描述及烧录、调试适配信息
├─ profiles/
│  └─ examples/                       # 无凭证的部署连接和物理拓扑示例
├─ examples/                          # 使用 SDK 的实验和自动化示例
├─ tests/
│  ├─ contract/                       # API、SDK 与节点契约一致性
│  ├─ integration/                    # 控制中心、存储、代理之间的集成
│  ├─ scenarios/                      # 完整开发调试闭环与异常路径
│  ├─ simulation/                     # 明确标识的模拟节点与故障注入
│  └─ hardware/                       # 真实硬件验证入口与台架定义
├─ deploy/                            # 控制服务与可选网关部署
├─ tools/                             # 开发、构建、迁移和打包入口
├─ .local/                            # 可配置的本机私有状态，不入版本库
└─ build/                             # 可再生的构建输出，不入版本库
```

`edbg_server`、`edbg_cli` 和 `edbg_gateway` 为工作名称，不冻结产品名称或实现语言。Python 是沿用现有服务端的候选起点，不约束微控制器代理的语言和工具链。

未开始的模块不创建空工程。`reference/` 已记录当前兼容查询接口，`agents/rp2/` 在实际适配时建立；工作流引擎、MCP 或额外 SDK 有明确需求后再增加。

## 主要依赖规则

```text
契约 ──> SDK ──> CLI / AI 适配 / 客户端脚本
  │
  ├──> 控制中心 API ──> 应用用例 ──> 领域规则
  │                         │
  │                         └──> 存储、节点链路和产物等适配
  │
  └──> 节点运行时 ──> 具体硬件或主机执行器

原始产物 + 分析参数 ──> 分析模块 ──> 带来源的派生产物
```

- `contracts/` 是契约定义的来源，不放数据库操作或硬件驱动。设备只使用所需子集。
- CLI 不直接读服务端数据库或导入服务端内部业务模块，公共访问通过 SDK 和接口。
- Web 不拥有独立的调度、权限或成功判定逻辑；它可以做输入反馈和专用数据展示。
- 控制中心不直接翻转现场 GPIO，具体执行在节点或网关执行器中。
- 执行器使用具体 SDK 和工具链，不能反过来依赖控制中心的存储实现。
- 公共分析模块通过明确输入运行，保留算法版本和来源，不隐式操纵硬件。
- 不为每个名词机械建立一层包；模块应按实际职责和变化原因聚合。

## 配置边界

| 内容 | 归属 | 约束 |
| --- | --- | --- |
| 硬件不可突破的边界 | `hardware/`、节点驱动 | 连接配置不能覆盖禁用引脚或驱动限制 |
| 动作与结果结构 | `contracts/` | 客户端、服务端和代理共享语义与版本 |
| 身份、接线、目标和资源关系 | 版本化部署配置与数据库 | 示例无凭证；实际部署状态与源码分开 |
| 运行时租约与操作 | 控制中心和节点运行时 | 不是静态配置，必须记录状态与证据 |
| 密钥、令牌、数据库及私人备份 | 私有状态目录 | 不进入示例、公开源码或普通分发包 |
| 固件、原始采集及转储 | 产物存储 | 内容引用、版本、校验及访问策略 |

未来私有状态路径由配置或环境指定，不绑定源码根目录。现有 `fleet/.state/` 在有迁移方案前继续保留。

## 迁移映射与后续拆分

| 现有位置 | 目标职责 | 迁移注意事项 |
| --- | --- | --- |
| `fleet/server.py` → `apps/control-plane/src/edbg_server/app.py` | 控制中心 | 已迁移并保留别名；用例、领域和存储仍待细分 |
| `fleet/debug_api.py` → `edbg_server/api/debug.py` | 会话与 UART 协调 | 描述已进入 `hardware/`；运行身份表与固件限制尚未迁出 |
| `fleet/i2c_api.py` → `edbg_server/api/i2c.py` | I²C 协调 | 保留原协议与校验；统一动作契约待定 |
| `fleet/wave_api.py` → `edbg_server/api/wave.py` | 采集协调 | 纯统计已提取到 `packages/analysis/`，输出保持兼容 |
| `fleet/firmware/main/agent.c` | ESP32 代理运行时与启动装配 | 身份、链路及代理更新不与单一应用绑死 |
| `fleet/firmware/main/debug_uart.c` | 会话运行时及 UART 执行器 | 避免以后打开任意工具都隐式启用无关外设 |
| `fleet/firmware/main/debug_i2c.c`、`wave.c` | 对应硬件执行器 | 板级限制与实际部署身份分开；维护本地检查 |
| `fleet/tools/client.py` → `packages/sdk-python/` | HTTP SDK | 新客户端显式认证；旧包装器保留本机凭证发现以兼容工具 |
| `fleet/tools/check_*.py`、`fleet/tests/` | 场景、契约、集成及硬件验证 | 将副作用、所需连接和证据产物写清楚 |
| `fleet/web/` → `apps/web/` | Web 客户端 | 已迁移；网页源码内容不变 |
| `fleet/deploy/`、`fleet/tools/` | 部署与开发入口 | 保留当前可运行方式，再增加新入口 |
| `fleet/.state/`、`fleet/artifacts/` | 私有状态与产物存储 | 先备份、迁移和校验，再调整路径及打包规则 |
| `simple_ota/` 及根目录旧工具 | 历史入门示例 | 保留独立说明，后续决定归档位置 |

## 渐进落地

第一批已建立兼容查询契约、SDK 与 CLI。接下来用三个代表场景收敛对象与最小动作契约。用旧实现的兼容适配承接第一批调用，逐项迁移到统一用例和执行器。

新旧入口不能维护两套互不相认的资源锁、控制权或操作账本。数据迁移需要版本、备份、字段核对和恢复路径；移除旧入口前应证明主要流程及异常语义一致。

目录移动不作为功能进度。首批只建立实际需要的模块，保留现有启动和部署方式；本批清单与验收依据见 [迁移验证](../validation/2026-10-27-workspace-batch-1.md)。
