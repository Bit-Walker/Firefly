# 连接配置示例

`esp32s3-two-node.json` 以占位身份说明两节点接线与角色。它是讨论和迁移用示例，不会被当前服务自动加载，也不是已经冻结的部署配置格式。

板级能力放在 `hardware/`，实际身份、连接和目标关系放在部署配置或数据库。令牌、Wi-Fi 密码和私钥放在独立私有状态中。

`workspace-target.json` 与 `workspace-connection.json` 是当前可显式 apply 的 v0 配置样例；先替换节点 ID，并在创建目标后引用其修订。具体方法见 [连接注册表说明](../../docs/reference/workspace-v0.md)。旧的 two-node 示例仍不会自动加载。
