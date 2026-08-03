# Aurora 能力 canary 候选运行手册

## 状态

此前部署的 session-token canary 已完成故障定位；本手册现描述的 access-token canary 配置仍是**尚未部署**的本地候选。它不代表 Aurora 2.5 多模态能力已经通过，更不代表自动续期已经修复，也不构成 FNOS 操作、真实 API 调用、重启或清理授权。

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

- Aurora 只挂载 `.secrets/canary/access_tokens.txt` 到容器内 `/home/nonroot/access_tokens.txt`，且容器内为只读；不得同时挂载 `session_tokens.txt`。
- Aurora 2.5.0 镜像以 `65532:65532` 运行；FNOS 上这份专用 canary Token 文件必须归属 `65532:65532` 并保持权限 `600`，不得通过放宽为 `644` 绕过读取失败。
- 该文件只允许包含经用户合法取得并专门放置的一行 ChatGPT access token；Codex 只核对存在性、大小、权限和挂载元数据，不读取、打印或复制正文。
- 该文件权限必须为 `600`，不能通过放宽到 `644` 来解决 UID/GID 问题。
- access token 快照不可续期，只用于隔离 canary 的即时能力验证，不得成为生产凭据权威。
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

在任何经批准的 canary 初始化或真实执行之前，受控且另行授权的初始化步骤必须预先创建 `data/canary/evidence/`，并核对其属主和权限。当前本地计划不创建该目录；工具刻意 fail closed，绝不自动创建缺失的父目录。

```bash
python3 scripts/aurora_capability_canary.py --allow-real-api --target direct --json
python3 scripts/aurora_capability_canary.py --allow-real-api --target both --output data/canary/evidence/latest-capability.json --json
```

能力矩阵 PASS 只证明该 access token 在本次调用时可支持对应能力。快照自然到期后的失败属于预期行为，不得把它解释为续期失败或成功；本方案不执行自然续期门禁，也不支持生产切换。若以后恢复 session/refresh 凭据路线，必须另行设计并验证真实换新与重启恢复。

## 停止与清理

任一门禁失败时，生产保持不变。是否保留或删除 canary 容器、独立数据、状态报告和候选镜像，均由独立清理授权决定；失败或本地验证结束不会自动触发清理。
