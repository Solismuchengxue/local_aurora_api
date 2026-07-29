# Solis_Aurora_Gateway 设计

状态：已采用
最近更新：2026-07-26

## 目标

- 在飞牛 fnOS 上提供可复现的 Aurora、New API、Mihomo 和 MetaCubeXD 组合部署。
- 只让 Aurora 的上游请求经过 Mihomo，不接管 NAS 系统流量。
- 把可共享配置、运行数据、敏感信息和第三方制品明确分开。
- 保留现有部署经验，同时避免在用户文档中混入维护记录。

## 正式架构

```text
OpenAI 兼容客户端
        │
        ▼
New API :3000
        │  Authorization: Bearer <ChatGPT access token>
        ▼
Aurora :8080
        │  PROXY_URL / http_proxy
        ▼
Mihomo :7890 ───── MetaCubeXD :9097
        │
        ▼
ChatGPT Web 上游

session_tokens.txt ── 定时续期脚本 ──▶ New API 渠道密钥
```

New API 是客户端入口；Aurora 负责协议转换；Mihomo 只处理 Aurora 显式发送的代理流量；MetaCubeXD 通过 Mihomo 控制端口管理配置。

## 目录职责

| 路径 | 职责 | Git |
|---|---|---|
| `docker-compose.yml` | 服务拓扑和固定运行参数 | 跟踪 |
| `.env.example` | 必填环境变量名称，不含真实值 | 跟踪 |
| `config/mihomo/` | 可共享的首次启动示例 | 跟踪 |
| `scripts/` | 无第三方依赖的运维脚本 | 跟踪 |
| `tests/` | 运维脚本回归测试 | 跟踪 |
| `data/` | Mihomo 与 New API 的运行数据 | 忽略 |
| `backups/` | 本地冷备份；仅跟踪目录说明 | 备份正文忽略 |
| `docs/` | 长期部署和排障文档 | 跟踪 |
| `assets/icons/` | WatchCow 可选图标 | 跟踪 |
| `.secrets/` | 本地凭据、续期锁和恢复副本 | 忽略 |
| `artifacts/` | 本地第三方安装制品 | 忽略 |
| `TODO.md`、`DEVLOG.md` | 当前行动和维护证据 | 忽略 |

## 关键边界

### 凭据

- New API 渠道密钥把 ChatGPT access token 传给已启用外部 token 的 Aurora。
- [Aurora 官方文档](https://github.com/aurora-develop/aurora#readme)支持 access、refresh 和 session token 账号池；当前镜像的账号池路径、权限和可用性实测不稳定，因此正式路径不依赖账号池。
- 定时任务经 Mihomo 使用 session token 换取新 access token，直测 Aurora 后以 SQLite 事务更新 New API 渠道密钥，重启 New API 清理缓存，再通过现有客户端令牌验证模型列表、鉴权和聊天响应结构。上游偶发的 HTTP 200 空正文不等同于 token 失效，不应触发凭据回滚。
- `.secrets/access_tokens.txt` 和 `.secrets/session_tokens.txt` 只作本地续期输入与恢复副本，不挂载到容器，不得提交、打印或外发。
- New API 的 `SESSION_SECRET` 只允许存放在被忽略的 `.env` 中。

### 运行数据

- `data/mihomo/` 会被订阅更新和 WebUI 修改。
- `data/new-api/` 包含 New API 数据库。
- 修改或清理 `data/` 前必须先备份；不得把真实运行数据复制进共享配置。

### 网络

- 不启用 Mihomo TUN，避免改变 NAS 的系统路由。
- Aurora 通过 Compose 内部服务名 `mihomo:7890` 使用代理。
- `7890` 不发布到主机；`9090` 通过 `NAS_LAN_IP` 仅绑定 NAS 的局域网 IPv4 地址，供浏览器中的 MetaCubeXD 连接。
- WSL 仅承担静态配置验证，不在本机启动或测试 Mihomo；代理出口以 NAS 实机结果为准。
- Win11 的 Clash Verge 是独立的本机代理环境，不属于本项目运行拓扑。

### 能力边界

- 日常对话使用 `gpt-5-6-pro`，复杂分析、代码和规划通过切换到 `gpt-5-6-thinking` 实现。
- 当前 Aurora 构建会拒绝 `reasoning_effort` 和 `reasoning.effort`；它们不是已采用的思考强度控制方式。
- `gpt-5-6-thinking` 不向第三方客户端返回可见的 `reasoning_content`，不能把思考档描述成可查看思维链。
- `gpt-5-6-pro` 和 `gpt-5-6-thinking` 均可触发 ChatGPT 原生联网搜索；该能力来自上游模型，不是客户端搜索工具。
- 图片生成虽有 Aurora 接口，但当前部署的真实 `/v1/images/generations` 请求仍以 HTTP 403 失败于 `sentinel prepare failed`，因此不是可用能力；New API 的渠道模型测试显示成功不能作为出图证据，正式配置已从渠道、客户端令牌范围和 abilities 中隐藏 `gpt-image-2`。
- Deep Research 没有一键端点；该低频能力不纳入正式架构，项目不提供相关脚本。

### 第三方组件

- 容器镜像固定到 2026-07-27 NAS 已验证运行的不可变 digest，没有引入新的软件包。
- WatchCow 是可选第三方增强，不是核心运行依赖。
- 不直接使用可漂移的 `latest` 标签。升级时一次只更新一个服务的 digest，并在 NAS 完成运行状态、数据、代理出口和 API 验证后再接受新基线。

## 共享文档

- [飞牛 fnOS 部署指南](docs/fnos_deployment.md)
- [WorkBuddy 自定义模型踩坑指南](docs/workbuddy_custom_models.md)

## 已知待验证项

- 2026-07-26 的 NAS 只读复验确认：旧部署目录中的 Compose 可以解析，四个目标容器均在运行，Mihomo 保持 GLOBAL 模式且出口国家为新加坡，New API 鉴权后的模型列表包含两个聊天模型。
- 2026-07-27 已完成到 `local_aurora_api` 的受控切换；四个容器的 Compose 工作目录和持久化挂载均指向新结构，GLOBAL、新加坡出口、模型列表和最小聊天请求均验证通过。
- 2026-07-29 已完成到 `/vol1/1000/Solis_Aurora_Gateway` 的运行路径切换；四个容器、两处持久化挂载和用户 cron 均使用新路径，本地端口 4/4 通过。该次路径切换没有复验代理出口、模型或真实聊天；历史路径已在最终备份和退役门禁通过后删除。
- 首次切换曾通过本地 override 挂载旧 Aurora 账号池，同时保留外部 token；该挂载不再作为定时续期的正式依赖。
- session token 本身可以经新加坡代理换取新 access token，但 Aurora 当前构建无法稳定使用 session/access-token 账号池，因此采用外部定时脚本更新 New API 的 SQLite 渠道记录。
- 旧目录 `aurora-stack` 已于 2026-07-29 在确认无容器、cron 或进程引用后删除；已校验的冷备份迁入 `backups/legacy/`，用于必要时回滚。
- Mihomo 示例配置只用于首次启动；导入订阅后的真实配置以 `data/mihomo/config.yaml` 为准。
- 当前 Compose 已固定四个已验证镜像 digest；尚未执行任何镜像升级，未来升级仍需逐项受控试验。
- 脱敏健康状态接入 n8n 的工作推迟到改革部全部迁移完成后再单独评审和批准；n8n 当前不是本项目运行依赖。

## 更新触发

- 服务职责、端口、数据卷或凭据流向变化时更新本文。
- 用户安装、启动或使用方式变化时同步更新 `README.md` 和部署指南。
- 失败尝试、验证证据和内部判断记录到本地 `DEVLOG.md`。
