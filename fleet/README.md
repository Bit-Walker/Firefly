# 节点固件与部署入口

项目总入口见 [根目录说明](../README.md)，当前状态见 [工程状态](../docs/current-state.md)。

- firmware/：ESP32-S3 fleet_agent 源码，当前构建版本 4.0.0，运行信息协议 edbg.agent/v1。
- tools/：固件编译、首次登记、服务启停与分发；部分早期台架脚本未纳入当前统一命令，不作为公共入口。
- deploy/：自建服务配置。
- .state/：本机私有状态，不分发。
- artifacts/4.0.0/：当前应用、引导程序和调试符号。

节点身份、Wi-Fi、服务地址、凭证及 CA 首次写入 NVS。后续更新使用 [镜像目录与 node.update](../docs/reference/node-update-v0.md)，只发布应用镜像；不要用 OTA 覆盖分区表或 NVS。/api/jobs 不接受 OTA。

当前测试接线为双板串口交叉连接、B GPIO8 监听 A UART0，以及 GPIO9/10 I²C 共线。该接线属于开发现场，公共驱动与资源配置仍在按 [研发顺序](../docs/architecture/08-next-implementation-batch.md)解耦。不能把这套实验接线当成任意节点的产品配置。

[开发指南](../docs/guides/development.md) · [部署配置](DEPLOY.md)
