# 在你的服务器上手动部署

本地测试不依赖公网服务器。这里提供供你手动执行的部署配置，尚未在你的服务器上验证。

## 服务器准备

1. 选择一个子域名，例如 `esp.你的域名`，把 DNS A/AAAA 记录指向服务器。
2. 服务器安装 Docker Engine 和 Docker Compose。
3. 云安全组与主机防火墙只向外开放 TCP 80、443（以及你已有的管理入口）。设备不需要入站端口。
4. 上传 `fleet/artifacts/fleet-source-and-deploy.zip` 并按完整目录解压，或复制公开源码仓库。需要保留根目录 `.dockerignore`、`apps/`、`packages/analysis/`、`hardware/`、`contracts/drafts/operation-v0/request.schema.json` 和 `fleet/requirements.txt`、`fleet/deploy/` 的相对位置。只复制旧 `fleet/` 已不足以构建。不要复制本地 `.state` 或测试 CA 的私钥。
5. 如果迁移已有 SQLite，先停止本地服务并安全备份数据；设备的服务器地址和 CA 配置也要同步迁移。不要直接把不同环境的同名设备凭证混用。

## 启动命令

Compose 使用仓库根目录作为构建上下文，根 `.dockerignore` 仅允许服务运行所需的公开源文件进入构建。服务器终端进入 `fleet/deploy`：

```sh
cp .env.example .env
```

编辑 `.env`，填入自己的 `FLEET_DOMAIN` 和证书联系邮箱 `ACME_EMAIL`，然后执行：

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
```

Caddy 自动申请并续期域名 HTTPS 证书；API 的 8000 端口只在容器网络内部使用。
首次启动会生成管理员账号，可用以下只读命令查看：

```sh
docker compose exec api cat /data/admin.local.json
```

在浏览器打开 `https://你的子域名/` 登录。随后登记设备，保存界面仅显示一次的 token。
此版本只有单管理员，建议将管理访问额外限制到你的 VPN 或可信访问入口。

## 为设备配置公网服务

从稳定的 **3.1.1 或更高版本** 开始，设备可以使用固件内置的公共 CA 证书包验证域名 HTTPS。
将控制台返回的设备登记结果保存成本地 JSON（内容为该设备的 `id` 与 `token`），然后在电脑执行：

```powershell
python .\fleet\tools\provision.py --id s3-设备MAC --name "现场设备" --server https://你的子域名 --token-file C:\本地私密目录\enrollment.json --wifi-config C:\本地私密目录\config.h --public-ca-bundle
```

若使用企业私有 CA，用 `--ca C:\路径\root-ca.pem` 代替 `--public-ca-bundle`。
工具不会关闭证书验证。服务器地址和设备身份在 NVS 中，不嵌入共享应用固件。
首次安装仍需要本地 USB；已在本地环境测试过的板子，也需先更新其 NVS 中的服务地址与 CA。

公网现场需允许 DNS、HTTPS 和网络校时。设备端无需暴露 HTTP 服务，也不必配置每个现场的路由器端口转发。

## 验证与发布

1. 确认设备上线，版本与内存信息持续刷新。
2. 执行一次诊断和远程重启，等待任务显示成功。
3. 上传匹配的 `fleet_agent.bin`，先选一台测试设备升级。
4. 等待“新固件已启动，并通过联网自检”，检查日志和业务功能。
5. 再选择下一批设备发布。当前批次由你手动控制，尚未实现自动灰度暂停策略。

## 数据与维护

- `fleet_data`：SQLite、设备登记、固件和管理员账号。
- `caddy_data`：HTTPS 证书与账户数据。
- 定期备份这两个卷；SQLite 运行中应使用在线备份 API，或停止服务后备份。
- 升级服务前保留数据副本；当前没有数据库升级迁移框架。
- 不要执行 `docker compose down -v`，除非明确要删除持久化卷。
- 2026-10-27 已检查新目录下的 Compose 配置；本机 Docker 引擎未运行，尚未验证本批镜像构建，也未在远端申请证书或做跨互联网实测。

## 可选 JTAG 网关

如果后续确实需要断点和单步，在需要调试的现场放置一台常开的网关电脑，连接 ESP32-S3 原生 USB/JTAG。
网关运行 OpenOCD，调试者通过 VPN 或 SSH 隧道连接。保持 GDB/OpenOCD 只对本机或受控 VPN 开放。
这与 OTA/日志服务分开实施，当前没有自动部署或验证该通道。
