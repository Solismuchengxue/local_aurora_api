# Solis_Aurora_Gateway

Solis_Aurora_Gateway 是一套面向飞牛 fnOS 的 Docker Compose 部署配置，用于把 ChatGPT Web 能力通过 Aurora 转换为 OpenAI 兼容接口，并由 New API 统一管理令牌、渠道和模型。

项目包含以下服务：

- Aurora：把 ChatGPT Web 请求转换为 OpenAI 兼容 API。
- New API：统一 API 网关和客户端令牌入口。
- Mihomo：只为 Aurora 提供应用级出站代理。
- MetaCubeXD：Mihomo 的 Web 管理面板。

## 前置条件

- 飞牛 fnOS 已安装 Docker 和 Docker Compose。
- 当前用户能够运行 Docker。
- 已准备可用的 ChatGPT session token。
- 已准备 Mihomo 兼容的代理订阅。

## 首次启动

将项目放到 NAS，例如：

```bash
cd /vol1/YOUR_USER_ID/Solis_Aurora_Gateway
```

创建本地环境文件，填写随机 `SESSION_SECRET`、独立的 `AURORA_AUTHORIZATION` 服务密钥和 NAS 的局域网 IPv4 地址：

```bash
cp .env.example .env
openssl rand -hex 16
```

分别生成并填写 `SESSION_SECRET` 与 `AURORA_AUTHORIZATION`，再把 `NAS_LAN_IP` 设置为 NAS 的局域网 IPv4 地址，例如 `192.168.0.100`。不要提交或分享 `.env`。

初始化运行目录：

```bash
mkdir -p data/mihomo data/new-api .secrets
install -m 600 /dev/null .secrets/session_tokens.txt
cp config/mihomo/config.example.yaml data/mihomo/config.yaml
```

把 ChatGPT session token 写入 `.secrets/session_tokens.txt`，并把文件归属设置为容器用户 `65532:65532`。只在首次部署时复制 Mihomo 示例配置；已有配置时不要覆盖。

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

## Token 自然续期

唯一正式 Aurora 直接只读挂载 `session_tokens.txt`。Aurora 启动时用 Session Token 换取 Access Token，并由上游实现的后台健康检查自然续期；New API 渠道只保存 `AURORA_AUTHORIZATION` 服务密钥，不保存 ChatGPT Token。项目不再安装外部 Token 换新 cron，也不保留旧 Aurora 或 canary 作为回退实例。

## 一键健康检查

在 NAS 项目目录运行：

```bash
python3 scripts/check_stack_health.py
```

脚本只读检查四个容器、SQLite 渠道与正式服务密钥的一致性、Mihomo GLOBAL/新加坡出口、正式三模型范围和一次真实聊天链路。使用 `--json` 可输出机器可读结果；存在失败项时退出码为 `1`。脚本不会输出凭据或聊天正文。

## 已实测能力

2026-08-04 已在 FNOS 把唯一生产 New API 升级到官方 `v1.0.0-rc.23` 固定
digest。升级前完整冷备份、SQLite 迁移、登录、渠道和核心聊天门禁均已通过；旧
rc.21 镜像已清理，已验证升级备份继续保留。

同日经生产 New API 入口执行一次 14 项、零重试的脱敏真实探针，结果为 8 项
PASS、6 项 FAIL。按照“模型存在部分失败即整体隐藏”的规则，正式渠道、默认 Token
和 abilities 最终严格保留 `gpt-5-6-pro`、`gpt-5-6-thinking`、`whisper-1`。

| 能力 | 状态 | 使用方式 |
|---|---|---|
| 普通对话 | 可用 | 使用 `gpt-5-6-pro` |
| 复杂思考 | 可用 | 切换到 `gpt-5-6-thinking`；当前不能通过 `reasoning_effort` 调节 |
| 网页搜索 | 可用 | 两个聊天模型都可能自动触发 ChatGPT 原生联网 |
| 音频转写 | 可用 | 使用 `whisper-1` 调用 `/v1/audio/transcriptions` |
| 音频翻译为英文 | 可用 | 使用 `whisper-1` 调用 `/v1/audio/translations`；该端点固定输出英文 |
| `gpt-4o` 多模态 | 隐藏 | 非流式聊天、Responses 和视觉通过，但流式、文件及组合翻译存在失败，未保留该模型 |
| 图片生成与编辑 | 隐藏 | 生成和编辑通过，但图片变体失败，未保留 `gpt-image-2` |
| 语音合成 | 隐藏 | `tts-1` 返回媒体未通过 MP3 完整性验收 |
| 英文音频转中文 | 隐藏 | 转写后再调用聊天模型的组合链路发生语义不匹配 |
| 深度研究 | 无一键能力 | 不纳入正式架构，项目不提供相关脚本 |

详细现象、限制和错误见部署指南的“活端点能力实测”。

## 使用限制

- 本项目使用 ChatGPT Web 登录态，不是 OpenAI 官方 API。
- Session Token 仍可能被上游吊销或失效；Aurora 无法自行恢复时由健康告警通知，人工更新受保护文件。
- 思考档不会向第三方客户端返回可见的思考过程。
- 模型联网搜索的引用标记偶有乱码或截断。
- Mihomo `7890` 不向局域网发布：Aurora 通过 Compose 网络使用，Docker daemon 只通过宿主机 `127.0.0.1:7897` 回环映射使用；`9090` 仅绑定 `.env` 指定的局域网 IPv4 地址。
- `backups/` 只保存本地备份，备份正文不进入 Git；项目级备份必须排除该目录。
- Aurora、New API、Mihomo 和 MetaCubeXD 均固定到 NAS 已验证运行的 digest。后续升级仍须检查上游变更、完整备份对应 `data/`，并逐个服务更新和验证。
