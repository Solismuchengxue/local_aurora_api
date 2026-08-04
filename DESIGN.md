# Solis_Aurora_Gateway 设计

状态：已采用
最近更新：2026-08-04

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
        │  Authorization: Bearer <Aurora service key>
        ▼
Aurora :8080
        │  PROXY_URL / http_proxy
        ▼
Mihomo :7890 ───── MetaCubeXD :9097
        │
        ▼
ChatGPT Web 上游

session_tokens.txt ──只读挂载──▶ Aurora 内部换取与自然续期 Access Token
```

New API 是客户端入口；Aurora 负责协议转换；Mihomo 处理 Aurora 显式发送的代理流量，并通过宿主机回环端口只为 Docker daemon 提供镜像仓库代理；MetaCubeXD 通过 Mihomo 控制端口管理配置。

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

- New API 渠道地址固定为 `http://aurora:8080`，渠道密钥与 `.env` 中的 `AURORA_AUTHORIZATION` 服务密钥一致；New API 不保存 ChatGPT Token。
- 唯一正式 Aurora 使用官方 2.5.0 固定 digest，以 `65532:65532` 运行，并把 `.secrets/session_tokens.txt` 只读挂载到 `/home/nonroot/session_tokens.txt`。
- [Aurora 官方文档](https://github.com/aurora-develop/aurora#readme)说明 Session Token 会在启动时换取 Access Token，后台健康检查每 10 分钟续期已过期的 Session/Refresh 账号。正式路径采用该内建机制，不再安装外部换新 cron。
- `ENABLE_EXTERNAL_TOKEN=false`，客户端或 New API 不能再把 ChatGPT Access Token 作为 Aurora Bearer 凭据临时注入。
- Session Token 或服务密钥不得提交、打印或外发。受保护文件必须保持权限 `600`；Session Token 文件归属容器用户 `65532:65532`。
- New API 的 `SESSION_SECRET` 与 Aurora 的 `AURORA_AUTHORIZATION` 只允许存放在被忽略的 `.env` 中。

### 运行数据

- `data/mihomo/` 会被订阅更新和 WebUI 修改。
- `data/new-api/` 包含 New API 数据库。
- 修改或清理 `data/` 前必须先备份；不得把真实运行数据复制进共享配置。
- New API `rc.21 → rc.23` 会执行数据库迁移；必须停止 New API 后冷备份整个 `data/new-api/`。核心门禁失败时同时恢复 rc.21 digest 与完整冷备份，禁止仅回退镜像。

### 网络

- 不启用 Mihomo TUN，避免改变 NAS 的系统路由。
- Aurora 通过 Compose 内部服务名 `mihomo:7890` 使用代理。
- `7890` 不发布到主机；`9090` 通过 `NAS_LAN_IP` 仅绑定 NAS 的局域网 IPv4 地址，供浏览器中的 MetaCubeXD 连接。
- WSL 仅承担静态配置验证，不在本机启动或测试 Mihomo；代理出口以 NAS 实机结果为准。
- Win11 的 Clash Verge 是独立的本机代理环境，不属于本项目运行拓扑。

### 能力边界

- 唯一生产 Aurora 2.5.0 Session Token 路径及 New API rc.23 核心聊天链路已在 FNOS 验证；正式聊天模型仍为 `gpt-5-6-pro` 和 `gpt-5-6-thinking`。
- `gpt-5-6-thinking` 不向第三方客户端返回可见的 `reasoning_content`，不能把思考档描述成可查看思维链。
- `gpt-5-6-pro` 和 `gpt-5-6-thinking` 均可触发 ChatGPT 原生联网搜索；该能力来自上游模型，不是客户端搜索工具。
- Aurora 2.5.0 上游公开聊天、Responses、文件问答、图片生成/编辑/变体、TTS、语音转写和音频翻译接口；是否对外开放以每项真实生产探针为准，路由存在或模型列表不能代替能力验收。
- `scripts/new_api_multimodal_probe.py` 只允许固定生产 New API 地址，从 SQLite 在内存选取一枚可用客户端 Token，并输出固定 Schema 的脱敏 14 项报告。2026-08-04 的单次零重试生产矩阵为 8 PASS、6 FAIL：仅 `whisper-1` 的转写和原生翻译为英文完整通过并保留；`gpt-4o`、`gpt-image-2`、`tts-1` 因模型内仍有失败项而从渠道、默认 Token 和 abilities 隐藏。

### 第三方组件

- Aurora 固定到 2026-08-04 已核对的官方 2.5.0 digest；其余容器继续使用既有不可变 digest，没有引入新的软件包。
- New API Compose 与 FNOS 唯一生产容器均使用官方 `v1.0.0-rc.23` 多架构 manifest `sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695`；冷备份、SQLite 迁移、控制台、核心链路和多模态门禁已完成现场验证。
- WatchCow 是可选第三方增强，不是核心运行依赖。
- 不直接使用可漂移的 `latest` 标签。升级时一次只更新一个服务的 digest，并在 NAS 完成运行状态、数据、代理出口和 API 验证后再接受新基线。

## 共享文档

- [飞牛 fnOS 部署指南](docs/fnos_deployment.md)
- [WorkBuddy 自定义模型踩坑指南](docs/workbuddy_custom_models.md)

## 部署状态

- 2026-07-26 的 NAS 只读复验确认：旧部署目录中的 Compose 可以解析，四个目标容器均在运行，Mihomo 保持 GLOBAL 模式且出口国家为新加坡，New API 鉴权后的模型列表包含两个聊天模型。
- 2026-07-27 已完成到 `local_aurora_api` 的受控切换；四个容器的 Compose 工作目录和持久化挂载均指向新结构，GLOBAL、新加坡出口、模型列表和最小聊天请求均验证通过。
- 2026-07-29 已完成到 `/vol1/1000/Solis_Aurora_Gateway` 的运行路径切换；四个容器、两处持久化挂载和用户 cron 均使用新路径，本地端口 4/4 通过。该次路径切换没有复验代理出口、模型或真实聊天；历史路径已在最终备份和退役门禁通过后删除。
- 2026-08-04 已完成 Aurora 2.5.0 Session Token 生产切换：FNOS 只保留唯一 `aurora-stack` 正式拓扑，旧 Aurora、两个 canary、旧换新凭据副本和旧外部换新 cron 已按门禁清理。
- 最终收敛不保留旧 Aurora、canary 或其冷备份作为回退；`backups/` 只允许保存当前正式栈按维护门禁创建的恢复包。
- Mihomo 示例配置只用于首次启动；导入订阅后的真实配置以 `data/mihomo/config.yaml` 为准。
- Aurora 2.5.0 固定 digest 已在唯一正式容器运行并通过 Session Token 启动换取、只读挂载、本地健康和脱敏真实聊天验证。
- 2026-08-04 已完成 New API rc.23 生产升级、完整冷备份和数据库迁移；四容器保持原标签与挂载且重启次数为 0。14 项真实多模态矩阵只保留完整通过的 `whisper-1`，最终模型范围为 pro、thinking、whisper。
- 已完成 `scripts/write_n8n_health_status.py` 的 Windows 实现、模拟测试和 FNOS 部署；生产 cron 在 `Asia/Shanghai` 的 05:12、17:12 原子更新单一滚动 `latest.json`，不调用模型、聊天、代理出口或外部 API。
- 2026-08-04 在最终模型收敛后再次手工发布离线健康状态；Schema v1 的 containers、runtime_contract、local_tcp、database、refresh_log 五项均为 `PASS`，n8n 专用子挂载继续保持只读。

## n8n 离线健康状态边界

- Aurora 是自身运行状态的生产者和权威来源；Studio OS/n8n 只能消费状态，不能通过状态链路反向修改 Aurora、容器、数据库、Token、Compose 或 cron。
- 生产器固定汇总容器、运行契约、本地 TCP、SQLite/正式渠道服务密钥一致性和固定的内建续期模式五项检查，以原子替换方式发布单个 `latest.json`；文件最大 16 KiB，不生成状态历史。
- 本地 TCP 检查仍要求四个批准端口具有合法 Docker 发布绑定；由于 FNOS 主机不能稳定回环到 Mihomo 的指定 LAN 发布地址，Mihomo 服务可达性改用运行时发现的容器桥接 IPv4 验证，地址仅在内存中使用且不进入状态。
- 状态只允许固定 Schema、状态码、布尔值、计数、固定事件枚举和 UTC 时间，不包含凭据、连接串、邮箱、Cookie、敏感路径、业务正文、原始日志、命令输出或异常正文。
- 文件型状态生产、挂载、读取和校验不新增或复用 Credential；现有 SMTP Credential 只用于异常通知。2026-08-02 已完成 FNOS 现场验证、状态目录与 cron 部署，并在 `/exchange` 读写父挂载之上增加 `/exchange/ops/aurora-gateway` 专用只读子挂载；`docker inspect`、容器内失败写入哨兵和父目录独立读写均验证通过。
- `Solis Aurora Gateway Alert (Phase 1)` 已导入、发布并激活，时区为 `Asia/Shanghai`，在 05:15、17:15 读取状态。手工合成异常已验证邮件通知成功，真实 `PASS` 文件链已验证静默且未执行邮件节点；两次验证均未调用真实 Aurora API。激活时没有手工执行工作流，自动检查只由计划触发器启动。
- 活动目录只保留滚动 `latest.json`。该文件未来会随 Studio OS `data/` 进入本地恢复包和加密云备份，因此不得扩展为敏感字段或无界历史。

## 更新触发

- 服务职责、端口、数据卷或凭据流向变化时更新本文。
- 用户安装、启动或使用方式变化时同步更新 `README.md` 和部署指南。
- 失败尝试、验证证据和内部判断记录到本地 `DEVLOG.md`。
