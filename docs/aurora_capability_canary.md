# Aurora 能力 canary 候选运行手册

## 状态

此前 canary 已用于凭据、最小聊天链路和直接 Aurora 能力矩阵核对；现场容器的当前状态仍须在每次操作前重新只读核对。本手册现描述能力探针 v2 及后续 session-token 续期门禁的候选用法。截至 2026-08-04，直接路径仅取得部分通过证据，尚未满足生产切换条件；这不代表 Aurora 2.5 全部多模态能力已经通过，更不代表自然续期或重启恢复已经验证，也不构成后续 FNOS 操作、真实 API 调用、重启或清理授权。

本手册配套的当前实现包括 [能力报告 Schema v2](contracts/aurora-capability-canary-report-v2.schema.json) 和 [设计规格](superpowers/specs/2026-08-03-aurora-capability-first-upgrade-design.md)。[准备计划](superpowers/plans/2026-08-03-aurora-capability-canary-local-preparation.md) 与 Schema v1 保留为初始实现历史，不作为当前运行合同。

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

还必须由操作者预先放置一份非敏感、非软链接、16 kHz/单声道/16-bit PCM WAV 样本，例如 `data/canary/input/aurora-canary-zh.wav`。样本应只包含合成短句“今天是能力测试”，大小必须在 1 字节至 8 MiB 之间，且不得位于 `.secrets/`。探针不会生成、下载或复制该文件；缺失或格式不符时只报告固定错误码。传入该独立样本后，转录与翻译不再依赖本轮 TTS 是否成功。

```bash
python3 scripts/aurora_capability_canary.py --allow-real-api --target direct --audio-fixture data/canary/input/aurora-canary-zh.wav --json
python3 scripts/aurora_capability_canary.py --allow-real-api --target both --audio-fixture data/canary/input/aurora-canary-zh.wav --output data/canary/evidence/latest-capability.json --json
```

v2 固定输出 13 项检查：模型列表、Chat 非流式/流式、Responses 非流式/流式、Files、真实图片理解、图片生成/编辑/变体、语音合成、语音转写、音频翻译。图片理解以内联 data URL 附加确定性 PNG，并要求模型识别主色；图片生成、编辑和变体请求最长允许 180 秒。图片响应允许上游文档化的可选 `revised_prompt`，但报告不保留该字段。变体使用独立的 `/v1/images/variations` 路由。TTS 请求使用上游示例的 MP3；压缩音频只通过 stdin 交给系统现有 `ffprobe` 做解码验证，不保存媒体文件，也不把探测输出或音频正文写入报告。图片接口的失败只暴露 prepare/conversation/poll 等阶段码。HTTP `404` 会区分本地 `route_missing` 与 Aurora 包装的上游 `upstream_not_found`，错误正文始终受限且不进入报告。

## 2026-08-04 直接路径实测

本轮使用固定 Aurora 2.5.0 canary、loopback `18080`、只读 access-token 文件和既有新加坡代理执行；没有创建 New API canary，也没有修改生产栈。下列为完整矩阵与后续单项复验形成的**累计证据**，不是一份全部通过的单次报告：

- 累计通过 12 项：模型目录、Chat 非流式/流式、Responses 非流式/流式、Files、图片理解、图片生成、图片编辑、图片变体、语音合成、语音转写。
- 图片三项均返回可解码 PNG；图片变体在把超时上限调整到 180 秒后单项复验通过。`/v1/models` 未列出 `gpt-image-2`，因此模型目录检查只验证可用聊天模型，不能代替图片端点验收。
- 语音合成已通过：当初始探针请求 `wav` 时，Aurora 2.5.0 实际把 `wav` 映射为上游 AAC；现场曾得到 synthesize `404`，也曾得到被旧版 WAV-only 校验器拒绝的响应。探针改为官方示例 `mp3` 后，受控复验返回 HTTP `200`；`ffprobe` 从 stdin 确认可解码 MP3、24 kHz、单声道、28,800 字节。响应未保存，音频正文和 ffprobe 原始输出均未进入报告。此前 TTS FAIL 的根因是格式映射与探针校验合同不一致，并伴随一次上游瞬时 `404`，不是能力整体缺失。
- 音频翻译失败：Aurora 2.5.0 的 `Translations` 虽向共用处理器传入 `isTranslation=true`，但该参数随后未被使用；处理器仍调用与转写相同的 `TranscribeAudio`，且只访问 `/backend-api/transcribe`。这与现场 HTTP `200`、中文原文返回完全一致，根因为 Aurora 实现没有实际执行英译。探针保持按结果分类为 `translation_mismatch`，避免把特定上游版本缺陷写死进通用报告合同。
- “英文音频翻译成中文”已另以组合链路完成单项复验：固定非敏感英文 WAV 先调用 `/v1/audio/transcriptions`，再把内存中的转写结果交给 `gpt-4o` 翻译为简体中文。两次请求均返回 HTTP `200`，转写与翻译结构、预期语义及中文字符检查全部通过，且没有输出或保存正文。该结果是“语音转写 + Chat 翻译”的组合能力 PASS，不属于只能翻译为英文的 OpenAI 兼容 `/v1/audio/translations` 合同，因此不改变原生十三项矩阵仍为 12/13 的结论。
- direct 仍因音频翻译一项未全绿，按门禁未执行 gateway/New API 能力矩阵；生产切换、New API 暴露多模态模型、自动续期与重启恢复均继续保持未通过。

能力矩阵 PASS 只证明该 access token 在本次调用时可支持对应能力。快照自然到期后的失败属于预期行为，不得把它解释为续期失败或成功；本轮 access-token 能力阶段不执行自然续期门禁，也不支持生产切换。后续 session-token 路线必须按下述独立门禁验证真实自愈与重启恢复。

## Session-token 自然续期与恢复门禁

该门禁与上面的 access-token 能力矩阵分离，只有取得新的 FNOS/真实请求/重启授权后才能执行：

- 该候选使用独立 Compose 文件 `docker-compose.session-renewal-canary.yml`，项目和服务均为 `aurora-session-renewal-canary`，只暴露 loopback `127.0.0.1:18082`；不包含 New API、access-token 文件、数据卷或自动重启，不改变现有 `18080` access-token canary 或生产栈。
- 在任何发布前，只能在 WSL Bash 用下列非敏感占位值做静态 Compose 解析；不得使用真实 `.env.canary`，且 JSON 结果必须丢弃。此检查不执行 `pull`、`create`、`up`、`start` 或 `run`：

```bash
AURORA_CANARY_AUTHORIZATION='non-sensitive-session-renewal-key' \
docker compose -f docker-compose.session-renewal-canary.yml --profile session-renewal config --format json >/dev/null
```

- session canary 和探针只接受 `.env.canary` 中唯一一条严格的 `AURORA_CANARY_AUTHORIZATION=value`：行首必须直接是键名，value 必须非空、无引号、无行内注释、无首尾或嵌入空白、无 NUL，且不得包含 `$`。`$VAR`、`${VAR}`、`$$` 及任何 Compose 变量插值形式均为无效配置；quoted、commented、`export`、前导空格、重复或空值也均无效，必须在 Compose/真实请求门禁前修正。探针遇到任一无效形式会在发出请求前 fail closed 为 `unavailable`，不能据此启动自然续期窗口。
- 仅在 FNOS 前置核对、创建和真实请求均获分别授权后，操作者才可从 checkout 根目录以符合上述严格语法的既有受保护 `.env.canary` 运行一次探针；探针固定请求 `http://127.0.0.1:18082/v1/chat/completions`，并与既有 Aurora capability canary 一致，将该 value 精确作为 `Authorization: Bearer <service-key>` 的 service key 使用。探针不读取 session token、零重试，无论是否提供兼容标志 `--json` 都只输出一行固定字段的脱敏 JSON；规范命令保留该标志：

```bash
python3 scripts/aurora_session_renewal_probe.py --allow-real-api --env-file .env.canary --json
```

- 真实自然续期观察窗口内，模型请求总数最多三次：首次 `auth_failed`、首次失败后 12 分钟复核、首次失败满 15 分钟的最终复核。只有首个结果为 `auth_failed` 才启动该窗口；若 12 分钟复核为 `pass`，立即停止而不发第三次。bootstrap 探针和后续重启恢复验证都需要各自独立授权，且不计入这三次续期窗口请求。

| 脱敏分类 | 判定 | 后续动作 |
| --- | --- | --- |
| `pass` | HTTP `200`，JSON 的 `choices[0].message.content` 为去除首尾空白后非空的字符串。 | 作为该次受保护健康请求成功；若处于续期窗口，立即结束并记录自然续期通过。 |
| `auth_failed` | HTTP `401`，或 Aurora 固定的 `no available account of the requested type` 错误。 | 仅首个该结果启动 15 分钟窗口；不得换 token、调用 session 接口、重启或重建。 |
| `unavailable` | loopback 连接失败、成功/HTTP 错误响应正文读取时发生超时或连接/OSError，或无法从受保护 env 文件取得唯一且符合严格语法的 service key。 | 停止；这不是 token 到期或续期失败证据。 |
| `invalid_response` | 其他 HTTP 错误、非 JSON 正文或不符合成功结构的 JSON。 | 停止；不得把协议或上游异常归因于自然续期。 |
| `upstream_forbidden` | HTTP `403`。 | 停止；单独保留上游拒绝分类，不能归入 `auth_failed`。 |

- 仅使用用户合法放置的 `session_tokens.txt`，挂载到容器内 `/home/nonroot/session_tokens.txt`；宿主文件归属 `65532:65532`、权限 `600`，容器内只读。不得同时挂载 access/refresh 账号池，也不得读取、打印或复制正文。
- Aurora 2.5.0 在启动时用 session token 交换 access token；受保护请求鉴权失败后才把账号标记为 expired，默认 10 分钟 health check 只重试 expired 账号。到期前重复交换得到同一 access token 是预期现象，不是续期失败证据。
- 自然到期门禁使用预先批准、固定低频的受保护健康请求暴露失败；该真实请求本身仍须单独授权。从首次确认鉴权失败起最多观察 15 分钟，并确保跨过至少一个 10 分钟 health-check 周期；不增加高频 session 请求，不手工触发换新。
- 自愈后必须由下一次受保护健康请求恢复 PASS；随后在另一项独立授权中仅重启 canary Aurora，验证同一只读 session 文件可重新建立账号。生产环境不设置自动重启补偿。
- 任一门禁失败时只发布固定 Schema 的脱敏 FAIL，并由现有 n8n Aurora Gateway Alert 只读通知。不得修改凭据、移动 Token、重启或重建生产组件、恢复宿主机换新脚本、调用额外补偿接口；由用户人工处理。
- 自然续期和重启恢复都通过之前，不得把 session-token 账号池迁入生产，也不得暂停或删除当前回滚能力。

## 停止与清理

任一门禁失败时，生产保持不变。是否保留或删除 canary 容器、独立数据、状态报告和候选镜像，均由独立清理授权决定；失败或本地验证结束不会自动触发清理。
