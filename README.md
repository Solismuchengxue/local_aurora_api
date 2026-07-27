# Local Aurora API

Local Aurora API 是一套面向飞牛 fnOS 的 Docker Compose 部署配置，用于把 ChatGPT Web 能力通过 Aurora 转换为 OpenAI 兼容接口，并由 New API 统一管理令牌、渠道和模型。

项目包含以下服务：

- Aurora：把 ChatGPT Web 请求转换为 OpenAI 兼容 API。
- New API：统一 API 网关和客户端令牌入口。
- Mihomo：只为 Aurora 提供应用级出站代理。
- MetaCubeXD：Mihomo 的 Web 管理面板。

## 前置条件

- 飞牛 fnOS 已安装 Docker 和 Docker Compose。
- 当前用户能够运行 Docker。
- 已准备可用的 ChatGPT access token。
- 已准备 Mihomo 兼容的代理订阅。

## 首次启动

将项目放到 NAS，例如：

```bash
cd /vol1/YOUR_USER_ID/local_aurora_api
```

创建本地环境文件，填写随机 `SESSION_SECRET` 和 NAS 的局域网 IPv4 地址：

```bash
cp .env.example .env
openssl rand -hex 16
```

把生成的值写入 `.env` 的 `SESSION_SECRET`，并把 `NAS_LAN_IP` 设置为 NAS 的局域网 IPv4 地址，例如 `192.168.0.38`。不要提交或分享 `.env`。

初始化运行目录：

```bash
mkdir -p data/mihomo data/new-api
cp config/mihomo/config.example.yaml data/mihomo/config.yaml
```

只在首次部署时复制 Mihomo 示例配置；已有配置时不要覆盖。

启动服务：

```bash
docker compose up -d
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

服务入口：

| 服务 | 默认地址 |
|---|---|
| New API | `http://<NAS_IP>:3000` |
| MetaCubeXD | `http://<NAS_IP>:9097` |
| Aurora | `http://<NAS_IP>:8080` |
| Mihomo 控制端口 | `http://<NAS_IP>:9090` |

随后在 MetaCubeXD 中导入订阅并选择合适节点，再到 New API 中初始化管理员、添加 Aurora 渠道并同步模型。

完整步骤和排障说明见 [飞牛 fnOS 部署指南](docs/fnos_deployment.md)。

## 客户端配置

在 OpenAI 兼容客户端中填写：

| 字段 | 值 |
|---|---|
| Base URL | `http://<NAS_IP>:3000/v1` |
| API Key | New API 中创建的令牌 |
| Model | 从 Aurora 渠道同步得到的模型名 |

WorkBuddy 还需要关闭工具调用并避免 URL 自动拼接错误，详见 [WorkBuddy 自定义模型踩坑指南](docs/workbuddy_custom_models.md)。

## 可选：定时续期

项目提供不依赖第三方 Python 包的续期脚本。它每天由 NAS 的 cron 检查两次，只在 access token 剩余不足 72 小时时才使用本地 session token 换新；新 token 会先直连 Aurora 验证，再以 SQLite 事务更新 New API 渠道，并通过完整链路执行真实聊天验证。

更新失败时会恢复旧渠道密钥。此模式适用于本项目默认的单机 SQLite 部署，完整配置见部署指南的“ChatGPT token 定时续期”。

## 已实测能力

以下结论来自 2026-07-26 对 NAS 活端点的实际调用：

| 能力 | 状态 | 使用方式 |
|---|---|---|
| 普通对话 | 可用 | 使用 `gpt-5-6-pro` |
| 复杂思考 | 可用 | 切换到 `gpt-5-6-thinking`；当前不能通过 `reasoning_effort` 调节 |
| 网页搜索 | 可用 | 两个聊天模型都可能自动触发 ChatGPT 原生联网 |
| 图片生成 | 不可用 | New API 渠道测试可显示成功，但真实图片端点仍以 HTTP 403 失败于 `sentinel prepare failed` |
| 深度研究 | 无一键能力 | 不纳入正式架构，项目不提供相关脚本 |

详细现象、限制和错误见部署指南的“活端点能力实测”。

## 使用限制

- 本项目使用 ChatGPT Web 登录态，不是 OpenAI 官方 API。
- access token 会过期，需要在 New API 渠道中定期更新。
- 思考档不会向第三方客户端返回可见的思考过程。
- 模型联网搜索的引用标记偶有乱码或截断。
- `7890` 只供 Compose 内部的 Aurora 使用，不发布到 NAS；`9090` 仅绑定 `.env` 指定的局域网 IPv4 地址。
- 项目使用第三方容器镜像，升级前应检查上游变更并备份 `data/`。
