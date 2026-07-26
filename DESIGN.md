# Local Aurora API 设计

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
```

New API 是客户端入口；Aurora 负责协议转换；Mihomo 只处理 Aurora 显式发送的代理流量；MetaCubeXD 通过 Mihomo 控制端口管理配置。

## 目录职责

| 路径 | 职责 | Git |
|---|---|---|
| `docker-compose.yml` | 服务拓扑和固定运行参数 | 跟踪 |
| `.env.example` | 必填环境变量名称，不含真实值 | 跟踪 |
| `config/mihomo/` | 可共享的首次启动示例 | 跟踪 |
| `data/` | Mihomo 与 New API 的运行数据 | 忽略 |
| `docs/` | 长期部署和排障文档 | 跟踪 |
| `assets/icons/` | WatchCow 可选图标 | 跟踪 |
| `.secrets/` | 本地凭据备份，不参与运行 | 忽略 |
| `artifacts/` | 本地第三方安装制品 | 忽略 |
| `TODO.md`、`DEVLOG.md` | 当前行动和维护证据 | 忽略 |

## 关键边界

### 凭据

- 当前仓库中的目标 Compose 使用 New API 渠道密钥把 ChatGPT access token 传给 Aurora，不挂载本地账号池。
- [Aurora 官方文档](https://github.com/aurora-develop/aurora#readme)说明外部 access token 默认启用，并且可以同时挂载 `access_tokens.txt` 账号池。
- NAS 上仍在运行的旧 Compose 挂载了 `access_tokens.txt`；这属于旧部署差异，不代表当前仓库已经完成该凭据路径的迁移。
- `.secrets/access_tokens.txt` 只是本机备份，不挂载到容器，也不得提交。
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
- 图片生成虽有 Aurora 接口，但当前部署稳定失败于 `sentinel prepare failed`，因此不是可用能力。
- Deep Research 没有一键端点；该低频能力不纳入正式架构，项目不提供相关脚本。

### 第三方组件

- 容器镜像沿用现有部署中的上游镜像，没有引入新的软件包。
- WatchCow 是可选第三方增强，不是核心运行依赖。
- `latest` 标签存在上游漂移风险；在没有完成实际升级试验前，不擅自固定新版本。

## 共享文档

- [飞牛 fnOS 部署指南](docs/fnos_deployment.md)
- [WorkBuddy 自定义模型踩坑指南](docs/workbuddy_custom_models.md)

## 已知待验证项

- 2026-07-26 的 NAS 只读复验确认：旧部署目录中的 Compose 可以解析，四个目标容器均在运行，Mihomo 保持 GLOBAL 模式且出口国家为新加坡，New API 鉴权后的模型列表包含两个聊天模型。
- 2026-07-27 已完成到 `local_aurora_api` 的受控切换；四个容器的 Compose 工作目录和持久化挂载均指向新结构，GLOBAL、新加坡出口、模型列表和最小聊天请求均验证通过。
- 首次切换继续通过本地 override 挂载旧 Aurora 账号池，同时保留外部 token。旧目录 `aurora-stack` 与已校验冷备份暂时保留，用于观察期内回滚。
- Mihomo 示例配置只用于首次启动；导入订阅后的真实配置以 `data/mihomo/config.yaml` 为准。
- 镜像版本固定策略仍待一次受控升级试验后决定。

## 更新触发

- 服务职责、端口、数据卷或凭据流向变化时更新本文。
- 用户安装、启动或使用方式变化时同步更新 `README.md` 和部署指南。
- 失败尝试、验证证据和内部判断记录到本地 `DEVLOG.md`。
