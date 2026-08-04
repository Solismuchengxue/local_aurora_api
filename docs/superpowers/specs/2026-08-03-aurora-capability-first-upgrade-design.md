# Solis_Aurora_Gateway 能力优先升级设计

状态：设计方向已确认；直接能力 canary 已形成阶段性证据，session-token 自然续期与重启恢复尚待独立门禁，生产切换尚未实施

日期：2026-08-03

## 1. 背景与结论

现网由 New API 接收客户端请求，并把一个会过期的 ChatGPT `access_token` 作为 Aurora 渠道密钥传给旧版 Aurora。宿主机脚本再尝试用静态 ChatGPT `session_token` 换取更晚的 `access_token`，更新 New API SQLite 渠道并重启 New API。

2026-08-03 的受控诊断已经证明：通过现网同一 Mihomo 路径两次直接请求 ChatGPT session 接口均成功，但返回的 `access_token` 与 New API 当前渠道 Token 完全相同，JWT 到期时间也完全相同。失败不是网络、TLS、SQLite、容器重启或回滚造成的，而是静态 session 登录态在当时没有产生更晚的 access token；现有脚本因此按设计拒绝覆盖。该诊断没有持久化 Token、修改 SQLite 或重启容器。

继续只修补这条外部换 Token 链路，会把主要精力放在旧架构的凭据搬运上，同时继续隐藏图片、文件和音频能力。正式方向改为：

> Aurora 负责 ChatGPT Web 凭据生命周期和多模态能力；New API 只持有稳定的 Aurora 服务访问 key，继续负责客户端 API key、权限、配额、审计和统一入口。

这是一项候选升级设计，不改变以下现网事实：当前生产镜像、Compose、SQLite、cron、n8n 告警和客户端配置均保持原状，直到全部独立门禁通过。

## 2. 已确认事实与待验证声明

### 2.1 已确认的本项目事实

- 当前生产 Aurora 固定为旧 digest `sha256:e5c3b823...`，New API 固定为 `v1.0.0-rc.21` 对应 digest `sha256:428018a3...`。
- 当前 New API 已注册图片生成、图片编辑、图片变体、语音合成、语音转写和音频翻译六类路由；无凭据的只读路由探测返回 `401`，未知路径返回 `404`。这只能证明路由存在，不能证明真实能力成功。
- 2026-07-27 的真实图片请求已经穿过 New API 到达 Aurora/ChatGPT 链路，最终失败于上游 `403 sentinel prepare failed`。因此“图片不可用”不能归因于 New API 没有图片路由。
- 现有 `refresh_chatgpt_access_token.py` 会要求新 JWT 到期时间晚于旧 JWT；2026-08-03 的真实 session 交换没有满足此条件，因此安全失败。
- 现有 n8n 工作流只消费脱敏 `latest.json` 并告警，不执行换 Token、调用模型或修改 Aurora。
- Aurora 2.5.0 的账号恢复逻辑不是按 JWT 到期时间主动轮换：启动时用 `session_token` 交换 access token；受保护请求鉴权失败后把账号标记为 expired；默认每 10 分钟的 health check 只重试 expired 账号。到期前再次调用 session 接口得到同一枚 access token 符合该机制，不能单独判定续期失败。
- 当前上游实现没有把 session 接口返回的轮换后 session token 写回账号对象或凭据文件，也没有可确认的续期专项自动化测试；因此自然续期和重启恢复必须分别取得现场证据，不能由 README 声明替代。

### 2.2 上游当前声明，尚未在本项目验证

Aurora 上游 `main` 当前标记为 `2.5.0`。其官方 README/API 声明支持：

- Chat Completions、Responses 和 Files；
- 图片生成、图片编辑、图片变体；
- 语音合成、语音转文字、音频翻译为英文；
- access、refresh 和 session token 账号池；
- 每 10 分钟检查并续期已过期的 session/refresh 账号；
- 通过 `Authorization` 环境变量配置稳定的服务访问 key。

上述内容是候选能力清单，不是本项目的已验证生产能力。镜像 digest、CPU 架构、实际账号能力、代理兼容性、Token 持久性、New API 转发兼容性和重启恢复都必须在隔离 canary 中逐项验证。

### 2.3 2026-08-04 隔离 canary 阶段性证据

- 直接 Aurora 路径累计通过 13 项中的 12 项：模型目录、Chat 非流式/流式、Responses 非流式/流式、Files、图片理解、图片生成、图片编辑、图片变体、语音合成和语音转写。
- 图片能力由实际可解码 PNG 证明，不依赖 `/v1/models` 是否列出 `gpt-image-2`；当前模型目录没有该 ID，因此模型枚举不得作为图片能力代理。
- TTS 已通过受控复验：Aurora 把初始请求的 `wav` 映射为 AAC，而原探针只接受 PCM WAV，导致合法压缩音频也被误判。候选探针改为官方示例 `mp3` 后，现场返回 HTTP `200`；FNOS 现有 `ffprobe` 从 stdin 确认为可解码 MP3、24 kHz、单声道、28,800 字节，全程未落盘媒体。此前一次 synthesize `404` 应保留为上游瞬时失败证据，不能再概括为路由不存在或 TTS 不可用。
- 音频翻译已定位为 Aurora 2.5.0 实现缺陷：`isTranslation=true` 没有进入任何后续分支，最终仍调用 `/backend-api/transcribe`，所以 HTTP `200` 返回中文并非账号或 WAV 问题。当前 `main` 保持同一实现，不能靠换用未固定的最新镜像解决。
- 英文音频到简体中文已用固定合成 WAV 完成一次组合链路复验：`/v1/audio/transcriptions` 转写后，再由 `gpt-4o` 翻译内存中的文本；两个端点均为 HTTP `200`，脱敏结构与语义断言通过。该能力应明确建模为“转写 + Chat 翻译”，不能冒充 `/v1/audio/translations` 的原生目标语言能力，也不计入十三项原生矩阵。
- 以上是完整矩阵与后续单项复验形成的累计证据，不是一份全绿的单次报告。direct 仍因音频翻译实现缺陷未全绿，因此没有创建或调用 New API canary，也没有进入自然续期、重启恢复或生产切换。

上游资料：

- [Aurora README](https://github.com/aurora-develop/aurora#readme)
- [Aurora API](https://github.com/aurora-develop/aurora/blob/main/API.md)
- [Aurora VERSION](https://github.com/aurora-develop/aurora/blob/main/VERSION)
- [New API 官方仓库](https://github.com/QuantumNous/new-api)
- [New API 图片接口文档](https://github.com/QuantumNous/new-api-docs/blob/main/docs/en/api/openai-image.md)

## 3. 目标与非目标

### 3.1 目标

1. 保留 ChatGPT Web/Aurora 路线，不降级为仅聊天网关。
2. 恢复并验证 Aurora 当前公开的图片、文件和音频能力。
3. 把短期 ChatGPT access token 从 New API 渠道配置中移除。
4. 由 Aurora 内部账号池管理 ChatGPT 登录态，New API 只使用稳定服务 key。
5. 保留 New API 的客户端 API key、权限、配额、审计和统一入口职责。
6. 在首次自然续期和 canary 重启恢复均通过前，不切换生产。
7. 延续脱敏状态、只读 n8n 消费和异常邮件告警边界。

### 3.2 非目标

- 不替换为 OpenAI 官方 API，也不把 ChatGPT 登录态与官方 API 凭据混为一谈。
- 不修改 Aurora、New API、Mihomo 或 MetaCubeXD 上游源代码。
- 不在本轮安装、拉取、启动或升级任何镜像。
- 不在设计阶段读取、打印、复制或转换任何真实 Token、Cookie、Credential 或业务正文。
- 不因为上游文档声称支持就直接开放客户端能力。
- 不自动把客户端改为直连 Aurora；直连只作为隔离验收路径或另行批准的兼容性退路。
- 不在本规格中扩展 n8n 为执行器；n8n 仍只读状态并通知。

## 4. 方案比较与选择

### 4.1 方案 A：New API + Aurora 内部凭据池（采用）

客户端仍访问 New API。New API 的 Aurora 渠道密钥改为稳定的 Aurora 服务访问 key；Aurora 从只读挂载的 `session_tokens.txt` 建立内部账号池并承担 access token 获取和失败后的低频自愈。

优点：保留统一网关和完整能力；过期 access token 不再进入 New API；凭据生命周期与真正使用凭据的组件归一。

风险：上游账号池、自然到期后的自愈和重启恢复尚未在 FNOS 当前环境验证；上游不持久化轮换后的 session token，必须以 canary 证据决定是否可部署。

### 4.2 方案 B：继续由宿主机更新 New API 的 access token（拒绝为长期方案）

优点：变更少、回滚路径已经存在。

缺点：2026-08-03 已证实静态 session 交换可能只返回同一 access token；New API 被迫承担不属于网关的凭据生命周期；每次更新需要数据库写入和重启；不能解决旧 Aurora 的多模态能力缺失。

该方案只作为生产切换前的临时旧路径和回滚路径保留，不再追加复杂重试。

### 4.3 方案 C：客户端全面直连 Aurora（不作为默认架构）

优点：链路最短，最容易暴露 Aurora 原生能力。

缺点：丢失 New API 的客户端令牌、权限、配额、审计和统一入口；客户端迁移面扩大。

只有在“直接 Aurora 通过、New API 链路失败”且评估过可用 New API 版本仍无法无损转发某个端点时，才提出限定到该端点的直连方案，并单独获得批准。

## 5. 目标架构

```text
OpenAI 兼容客户端
        │  New API 客户端 API key
        ▼
New API :3000
        │  稳定 Aurora 服务访问 key
        ▼
Aurora 2.5 候选版本 :8080
        │  内部选择登录账号并管理 access token
        │  PROXY_URL / http_proxy
        ▼
Mihomo :7890
        ▼
ChatGPT Web 上游

FNOS 只读凭据源
  └─ session_tokens.txt
        │  仅 Aurora 容器可读
        └──────────────▶ Aurora 内部账号池
```

### 5.1 组件职责

| 组件 | 唯一职责 | 不承担 |
|---|---|---|
| New API | 客户端鉴权、权限、配额、审计、模型/渠道路由 | ChatGPT access token 换新 |
| Aurora | ChatGPT Web 协议转换、账号选择、凭据生命周期、多模态端点 | 客户端配额和业务审计 |
| Mihomo | Aurora 显式上游流量代理 | NAS 系统代理或 TUN |
| 宿主机运维层 | 备份、受控部署、脱敏状态发布 | 日常搬运 access token |
| Studio OS/n8n | 只读消费脱敏状态、异常通知 | 调用 Aurora、换 Token、修改配置 |

### 5.2 鉴权流向

1. 客户端只知道 New API 客户端 API key。
2. New API 渠道保存一个随机、长期稳定、只用于内部服务间鉴权的 Aurora 服务访问 key。
3. Aurora 通过 `Authorization` 配置同一服务 key，并使用内部账号池满足实际 ChatGPT 登录态要求。
4. `ENABLE_EXTERNAL_TOKEN` 在目标架构中关闭；是否使用精确值 `false` 必须由 canary 验证该版本行为后再固化。
5. New API 不再保存或转发 ChatGPT access token，现有 SQLite 渠道 JWT 仅保留在切换备份中用于回滚。

服务 key 和账号凭据是两类不同秘密，不得复用。服务 key 存放在被 Git 忽略的本机配置中；账号凭据只存放在 `.secrets/` 的专用文件中。

## 6. 凭据与文件权限设计

### 6.1 凭据来源

1. 目标方案只使用用户合法取得的 ChatGPT `session_token`，以官方 `session_tokens.txt` 账号池格式交给 Aurora；不并行引入 `refresh_tokens.txt`，也不把两种凭据混用。
2. 单独的短期 `access_tokens.txt` 只可作为能力 canary 或紧急回滚输入，不能成为目标生产凭据权威。
3. 容器启动、模型列表成功或到期前 session 交换返回同一 access token，都不能证明长期续期成功；必须等待自然到期后的真实自愈证据。

本项目不设计抓取、窃取或绕过登录流程获取 session token 的方式。没有合法可持续凭据时，生产切换被阻塞。

### 6.2 最小权限

- 凭据文件留在 Git 忽略的 `.secrets/`，宿主机目录不允许其他普通用户读取。
- Aurora 只把专用 `session_tokens.txt` 挂载到容器内 `/home/nonroot/session_tokens.txt`，不挂载整个 `.secrets/`，也不同时挂载 access/refresh 账号池。
- 不把凭据放入镜像、Compose 跟踪文本、命令行参数、日志或健康状态。
- Aurora 2.5.0 以 `65532:65532` 运行；专用文件必须归属 `65532:65532`、权限 `600`，并以只读 bind mount 提供。不得通过改成 `644` 或挂载整个目录绕过读取失败。
- 如果候选 Aurora 必须写回 session 文件才能在自然续期或重启后恢复，而只读挂载无法满足，则本设计的生产门禁失败。不得未经另行设计和批准把凭据目录改为读写。

### 6.3 续期与重启生存性

Aurora 的现有机制是“受保护请求鉴权失败后标记 expired，再由默认 10 分钟 health check 重试”，不是按到期时间高频主动换新。本项目接受这一低频机制，不新增宿主机 renewal supervisor，不按分钟主动调用 session 接口，不自动移动 Token，也不让 n8n 执行补偿。canary 必须依次证明：

1. 从批准的只读凭据源建立可用登录账号；
2. 在原 access token 自然到期且一次预先批准、固定低频的受保护健康请求暴露鉴权失败后，Aurora 能在首次失败起 15 分钟内、至少跨过一个 10 分钟 health-check 周期自行恢复；不得为触发续期而增加高频请求；
3. 只重启 canary Aurora 后，仍能从同一只读凭据源恢复并完成请求；
4. 全程没有扩大文件权限、输出凭据或依赖 New API 中的旧 JWT。

自然续期超过上述窗口仍未恢复时，或受控重启后不能恢复时，停止自动推进，只发布脱敏 FAIL 并通过既有 n8n 邮件链通知用户人工修补。不得自动修改凭据、重建或重启生产组件、调用额外补偿接口，亦不得恢复宿主机换 Token 方案。任一项失败都表示当前方案不适合生产切换；不得以跳过首次自然续期或重启恢复来换取上线速度。

## 7. 多模态能力验收矩阵

每项能力必须先直测隔离 Aurora，再通过隔离 New API 复测。只测试一个最小、无敏感内容的合成样本；不使用用户业务文件、真实聊天记录或私人音频。

| 能力 | Aurora 直测 | 经 New API | 关键验收 |
|---|---:|---:|---|
| 模型列表 | 必须 | 必须 | 鉴权有效、正式模型范围明确 |
| Chat Completions | 必须 | 必须 | 非流式结构合法；再验证流式结束 |
| Responses | 必须 | 必须 | 非流式结构与 SSE 事件序列合法 |
| Files + 文件问答 | 必须 | 必须 | 小型合成文本上传、返回 file id、问答成功 |
| 图片理解 | 必须 | 必须 | 上传确定性合成 PNG，模型正确识别主色；不接受提示词回显 |
| 图片生成 | 必须 | 必须 | `gpt-image-2` 产生可解码图片；不接受仅渠道测试成功 |
| 图片编辑 | 必须 | 必须 | 对合成源图执行明确变更并得到可解码图片 |
| 图片变体 | 必须 | 必须 | 无编辑提示的变体调用得到可解码图片 |
| 语音合成 | 必须 | 必须 | 非空音频、MIME/容器格式正确、可解码 |
| 语音转文字 | 必须 | 必须 | 独立的非敏感 WAV 样本转写得到预期短句，不依赖同轮 TTS |
| 音频翻译为英文 | 必须 | 必须 | 同一独立非英语 WAV 样本得到语义相符英文文本 |

对图片和音频，不以 HTTP `200` 或非空 body 单独判定成功；必须校验返回结构、媒体格式和最小可解码性。对 URL 返回必须限制域名并禁止在报告中输出带签名 URL。对 base64 返回只做长度、解码和媒体头检查，不打印正文。

真实能力调用会在 ChatGPT 上游产生请求和可能的临时文件，必须在对应 canary 门禁中再次明确批准。若上游没有删除接口，应在测试前披露无法主动清理的远端最小测试制品。

## 8. New API 兼容策略

当前 New API 的六类图片/音频路由存在不等于转发兼容。隔离验收重点检查：

- `multipart/form-data` 文件字段是否原样传递；
- 图片 URL、base64 与 SSE 是否被改写或截断；
- 音频二进制响应的 `Content-Type`、长度和内容是否保持；
- Responses SSE 事件是否保持顺序和结束标记；
- 请求体大小、超时、模型映射、渠道权限和客户端 Token 范围是否正确；
- New API 错误是否保留足够的脱敏分类，而不泄露上游正文。

判定：

- Aurora 直测失败：属于 Aurora、账号、上游或代理候选失败，不能归因于 New API。
- Aurora 直测通过、New API 失败：属于网关版本、渠道配置或协议转发问题；先评估受控升级 New API。
- 两条链路均通过：才允许在正式渠道和客户端能力范围中恢复该能力。
- 所有合理 New API 候选仍失败而 Aurora 稳定通过：停止自动推进，提交“限定端点直连或继续隐藏”的单独架构决策给用户。

## 9. 健康状态与 n8n 迁移

现有 `latest.json` 的 `database` 检查把 New API 渠道密钥当作 JWT 并检查到期时间。目标架构中渠道密钥变为稳定服务 key，该语义必须升级，不能继续报告 `token_near_expiry`。

### 9.1 状态生产原则

- Aurora 仍是自身运行状态权威；n8n 只读消费。
- 保留单一滚动 `latest.json`、固定 Schema、原子替换、16 KiB 上限和无敏感字段边界。
- 不把 Token、哈希、Cookie、Credential ID、邮箱、连接串、挂载源、原始日志、错误正文、上游响应或业务内容写入状态。
- n8n 不新增 Aurora Credential，不直接调用 Aurora，也不执行换新。

### 9.2 Schema 演进

生产切换时发布新的 Schema 版本，至少区分：

- New API 渠道是否存在且启用；
- 渠道是否处于固定的 `aurora_service_key` 模式；
- New API 与 Aurora 的服务 key 是否只在内存中完成一致性比较；状态只输出布尔值；
- Aurora 账号池文件挂载是否存在、是否只读、是否满足批准的最小权限；
- 最近一次受控能力探测是否成功、是否过期；
- 首次自然续期是否已经观察通过；
- 最近一次自然续期/重启恢复门禁的结果枚举，仅允许 `not_observed`、`pass` 或 `fail`，不得包含凭据、响应或错误正文。

Aurora 上游目前没有文档化的脱敏账号池健康端点。本项目不得虚构“当前 access token 到期时间”或从自由文本日志猜测成功。若没有稳定的第一方机器可读信号，状态只能根据受控健康请求和观察窗口诚实报告“最近受保护能力探测结果”与“自然续期/重启恢复是否已验证”，不能声称知道 Aurora 内部 Token 状态或内部换新次数。

### 9.3 工作流兼容

新 Schema 的生产器、验证器和 n8n 工作流导出必须先在 Windows 完成测试。实例工作流在生产切换前保持现状；切换窗口中，生产器与工作流必须作为同一兼容性变更受控发布，避免旧验证器把新 Schema 当成损坏文件。

异常邮件仍复用现有 SMTP Credential，仅在实例内绑定；正常状态继续静默。自然续期或重启恢复失败时由 Aurora 健康生产器发布脱敏 FAIL，现有 Aurora Gateway Alert 只读消费并通知；n8n 不读取凭据、不调用 Aurora、不执行重试或修复。工作流、SMTP、数据库、Compose 和 cron 的每类现场变更仍保持独立批准。

## 10. 隔离 canary 设计

### 10.1 隔离边界

- 使用独立容器名、Compose 项目标识、内部网络、宿主机端口和数据目录。
- Aurora 候选镜像必须固定为已核对来源、版本、架构和 SHA/digest 的不可变引用。
- New API canary 首先使用与生产相同的固定版本，以判断问题是否来自 Aurora；只有证据表明网关不兼容时才评估 New API 升级。
- 不复制生产 New API SQLite；canary 使用全新最小数据库、测试管理员、测试渠道和测试客户端 Token。
- 不停止、重建或重命名生产四容器，不修改生产 Compose、SQLite、cron 或 n8n。
- canary 的数据面和 New API 使用独立网络。由于生产 Mihomo 的 `7890` 不发布到宿主机，Aurora canary 可在另行批准后额外挂接现有 `aurora-stack` 内部网络，只访问 `mihomo:7890`；该方式不会重建生产容器，但并不构成与生产网络的硬隔离，现场验收必须明确记录这一风险。若用户要求网络级硬隔离，则改为单独设计一个不发布端口、只读使用既有 Mihomo 配置的 canary 代理，不得临时开放生产 `7890`。

### 10.2 canary 顺序

1. 静态核对候选镜像来源、许可证、版本、架构、digest 和配置差异。
2. 创建 Aurora-only canary，验证启动、账号池读取、稳定服务 key 和直接能力矩阵。
3. 创建隔离 New API canary，验证客户端 key、渠道服务 key和完整转发矩阵。
4. 改用只读 `session_tokens.txt` 的独立 canary，保持运行直到内部 access token 自然到期；由预先批准、固定低频的受保护健康请求暴露失败，并观察最多 15 分钟、至少一个 health-check 周期，验证 Aurora 自愈，不增加主动换新或补偿调用。
5. 在另行批准的受控门禁中只重启 canary Aurora，验证同一只读 session 凭据源可以恢复；生产不配置自动重启补偿。
6. 输出只含布尔值、枚举、时间和计数的脱敏报告；随后由用户决定是否进入生产切换计划。

任何阶段失败，停止自动推进，保留脱敏状态证据并通知用户；生产不变。不得自动修改凭据、重启/重建组件或执行补偿方案。canary 清理也是独立授权，不因失败自动删除证据。

## 11. 生产切换与回退设计

### 11.1 生产切换前置条件

以下条件必须全部满足：

- Aurora 直测与 New API 全能力矩阵通过；
- 首次自然续期通过；
- canary Aurora 重启恢复通过；
- 凭据文件保持非 world-readable，挂载只读；
- 新健康 Schema 与 n8n 验证器测试通过；
- 当前 New API 数据和 Compose 已创建并校验最终备份；
- 已形成只影响 Aurora/New API 和两条 Aurora cron 的精确回退命令；
- 用户另行批准生产维护窗口。

### 11.2 切换顺序

1. 冻结 Aurora/New API 相关配置写入并创建最终备份。
2. 暂停旧 `refresh_chatgpt_access_token.py` cron，避免它把稳定服务 key 当成 JWT 或覆盖渠道。
3. 部署固定 digest 的 Aurora 候选、只读账号池挂载和稳定服务 key。
4. 把 New API 的 Aurora 渠道密钥从旧 access token 改为稳定服务 key。
5. 发布与新架构匹配的健康生产器和 n8n Schema/验证器；保留原计划时区。
6. 只重建确实需要配置变化的 Aurora/New API；不重建 Mihomo、MetaCubeXD、n8n 或 PostgreSQL，除非其自身变更另获批准。
7. 依次验证容器、代理、直接 Aurora、New API 全能力链、客户端权限和脱敏状态。
8. 进入观察期；旧脚本、旧配置和最终备份暂不删除。

### 11.3 回退

任一门禁失败时：

1. 停止新增请求验证；
2. 恢复旧 Aurora digest、旧 Compose 和旧 New API 渠道 access token；
3. 恢复旧健康生产器/Schema 与旧 refresh cron；
4. 只重建 Aurora/New API 并验证旧聊天链路；
5. 保留失败 canary/切换的脱敏证据，不删除最终备份。

回退不得删除 New API 数据、其他工作流、SMTP Credential、Studio OS 数据、旧最终备份或 Mihomo 运行配置。

## 12. 测试与验收

### 12.1 Windows 本地

- 为新凭据模式、Schema 演进、旧 Schema 拒绝/兼容窗口和脱敏规则编写回归测试。
- 使用非敏感占位值运行 `docker compose config`。
- 检查所有 Markdown 相对链接、`git diff --check` 和 ignore 规则。
- 确认 `.env`、`.secrets/`、`data/`、测试媒体输出、TODO/DEVLOG 和临时 bundle 均未跟踪、未暂存。
- 不在 WSL 启动 Aurora/Mihomo 或执行真实请求。

### 12.2 FNOS canary

- 容器来源、digest、架构、用户 UID/GID、挂载只读和代理路径均符合设计。
- 十三项检查在直接 Aurora 和 New API 两条路径全部通过。
- 响应中不输出凭据；测试报告不包含业务正文、签名 URL、媒体 base64 或原始日志。
- 原 access token 自然到期后仍能使用；canary 重启后仍能恢复。
- 自然续期失败时最多等待已定义的 15 分钟观察窗，此后状态为脱敏 FAIL；没有宿主换新、自动重启或额外补偿调用。
- 生产容器、Compose 标签、挂载、cron、SQLite、n8n 与 Git 状态无变化。

### 12.3 生产验收

- New API 客户端仍使用原入口和客户端 API key。
- New API 渠道不再保存 ChatGPT JWT；Aurora 服务 key 匹配但不输出值。
- 图片、文件和音频能力只在实测通过后进入渠道模型、客户端 Token 范围和 abilities。
- 旧 refresh cron 已暂停且未被替代为新的宿主换新任务；新状态生产与 n8n 告警按 Asia/Shanghai 计划运行。
- 正常状态静默，合成异常可通知；不得用合成执行代替首次自然续期证据。
- 观察期结束前不删除旧脚本、备份或回退资料。

## 13. 独立批准门禁

1. 本设计规格复核。
2. 详细实施计划复核。
3. Windows 本地代码、测试和文档实现。
4. commit 与 push。
5. FNOS 只读现场基线核对。
6. 候选镜像拉取与隔离 canary 创建。
7. 使用合成内容执行真实图片、文件、音频和聊天能力测试。
8. 首次自然续期与 canary 重启观察。
9. 生产备份、维护窗口和切换。
10. 生产观察完成后的旧 refresh 路径退役与清理。

前一门禁通过不自动授权后一门禁。任何“继续”只在当前明确阶段内生效。

## 14. 实施分解

本规格是总架构，不应生成一个跨越本地开发、真实能力调用和生产切换的单一大计划。用户复核后按以下顺序分别形成计划，每个计划结束后重新验收并等待下一门禁：

### 实施制品

本地准备阶段的当前可审查制品为：[能力报告 Schema v2](../../contracts/aurora-capability-canary-report-v2.schema.json) 和 [候选运行手册](../../aurora_capability_canary.md)；[准备计划](../plans/2026-08-03-aurora-capability-canary-local-preparation.md) 与能力报告 Schema v1 保留为初始实现历史。这里的能力报告 Schema v2 与生产健康 `latest.json` Schema v2 是两个不同合同；后者仍因 canary 尚未建立稳定的第一方续期信号而延期。

1. **本地准备计划**：候选版本/许可证/digest 核对，Compose 候选覆盖，健康 Schema 与测试，文档差异；不连接 FNOS。
2. **FNOS 隔离 canary 计划**：只读基线、镜像拉取、独立容器/数据、真实能力矩阵、首次自然续期和重启恢复；不修改生产栈。
3. **生产切换计划**：最终备份、旧 refresh cron 暂停、Aurora/New API 凭据流向切换、状态/n8n 兼容发布、验收与回退。

任一计划发现总架构假设不成立时，先修订本规格并重新获得用户确认，不把未解决问题推到后续阶段。

## 15. 设计决策摘要

- 采用能力优先，而不是围绕旧外部 access token 更新链继续加补丁。
- 保留 New API，但把它收敛为客户端网关；不让它继续持有短期 ChatGPT JWT。
- Aurora 成为 ChatGPT Web 凭据和能力权威，前提是只读 session-token canary 证明官方低频自愈和重启恢复真实可用。
- 图片/音频不可用不能再仅由渠道测试、路由存在或文档声明判断，必须同时通过直接 Aurora 和 New API 两条真实链路。
- 在首次自然续期通过前，生产不切换；如果无法获得合法可持续凭据，明确阻塞而不是假装自动续期成功。
- 不新增宿主 renewal supervisor、主动高频换新或自动重启补偿；失败只发布脱敏 FAIL 并通知用户人工修补。
- n8n 继续只读告警，不获得 Aurora Credential，也不执行任何换新或模型调用。
