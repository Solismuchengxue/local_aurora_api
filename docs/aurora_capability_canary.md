# Aurora 能力 canary 候选运行手册

## 状态

这是本地准备阶段形成的候选运行手册，**尚未部署**。它不代表 Aurora 2.5、自动续期或任何多模态能力已经通过，也不构成 FNOS 操作、真实 API 调用、重启或清理授权。

本手册配套的本地实现包括 [准备计划](superpowers/plans/2026-08-03-aurora-capability-canary-local-preparation.md)、[能力报告 Schema](contracts/aurora-capability-canary-report-v1.schema.json) 和 [设计规格](superpowers/specs/2026-08-03-aurora-capability-first-upgrade-design.md)。

## 安全边界

- 生产 Compose、容器、SQLite、cron 和 n8n 均保持不变。
- canary 仅以 loopback 端口 `18080`（Aurora）和 `13000`（New API）对宿主机暴露服务。
- Aurora canary 额外挂接生产内部网络，仅为访问 `mihomo:7890`；这不是网络硬隔离，仍须把它当作可经由生产代理访问上游的候选容器。
- 真实调用使用最小合成内容。Files、图片和音频请求仍会在 ChatGPT 上游产生副作用；若上游没有删除接口，测试制品不能主动清理，必须在授权前披露这一限制。

## FNOS 前置只读核对

取得单独授权后，先以只读方式核对以下事实，再决定是否提出 canary 创建申请：

1. NAS CPU 架构及候选镜像是否兼容。
2. 生产内部网络的精确名称、`18080`/`13000` 端口占用情况。
3. 容器运行 UID/GID 及其对只读凭据文件的可读性。
4. secret 文件的存在性、属主/组和权限元数据；不读取 secret 正文。
5. 当前生产容器、Compose、SQLite、cron、n8n 和健康状态的基线。

候选镜像还须在拉取前独立核对 Aurora 官方来源、`VERSION`、MIT license、`linux/amd64` manifest 与不可变 digest。镜像拉取需要另行批准。

## Secret 与初始化门禁

- Aurora 只挂载 `.secrets/canary/session_tokens.txt`，且容器内为只读。
- 该文件不得 world-readable；不能通过将权限放宽至 `644` 来解决 UID/GID 问题。
- New API canary 使用独立的空数据库；其 Aurora 渠道 base URL 必须精确为 `http://aurora-canary:8080`，渠道 key 为稳定的 Aurora 服务 key。
- 客户端 token 仅存于 `.secrets/canary/new_api_client_token.txt`，不得写入 Compose、报告、日志或命令行。

## 经批准后的真实执行顺序

以下仅是未来取得相应授权后的命令示例。执行 `pull`、`up`、真实探针、重启和清理均不在本地计划授权范围内。

先在 WSL 的 Bash 中只用**本次命令注入**的非敏感占位值静态解析；不使用 `--env-file`，不得读取真实 `.env.canary`。将 JSON 输出重定向到 null，禁止把解析后的环境变量或任何 secret 输出到终端、日志或报告：

```bash
AURORA_CANARY_IMAGE='ghcr.io/aurora-develop/aurora@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
AURORA_CANARY_AUTHORIZATION='non-sensitive-canary-service-key' \
NEW_API_CANARY_SESSION_SECRET='non-sensitive-canary-session-secret' \
docker compose -f docker-compose.canary.yml --profile canary config --format json >/dev/null
```

Windows 主机应通过 WSL 运行同一条命令；该检查只验证 Compose 语法，不拉取、创建或启动任何容器。

在 isolated canary 已创建且获得每次真实调用的独立批准后，必须先执行 direct；仅在全部 PASS 后再执行 both：

```bash
python3 scripts/aurora_capability_canary.py --allow-real-api --target direct --json
python3 scripts/aurora_capability_canary.py --allow-real-api --target both --output data/canary/evidence/latest-capability.json --json
```

能力矩阵 PASS 只证明该次工具调用的能力结果，不等于自动续期通过。自然续期门禁要求保持 canary 至旧 access token 自然过期后复测，再单独重启 canary Aurora 并复测；两项均须在生产切换前得到独立的真实证据。

## 停止与清理

任一门禁失败时，生产保持不变。是否保留或删除 canary 容器、独立数据、状态报告和候选镜像，均由独立清理授权决定；失败或本地验证结束不会自动触发清理。
