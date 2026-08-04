# New API rc.23 生产直升与多模态验收设计

## 目标

把 FNOS 唯一生产 `new-api` 从官方 `v1.0.0-rc.21` 升级到
`v1.0.0-rc.23`，随后通过 New API 客户端入口完成现有聊天回归和多模态
端到端验收。只把真实请求通过的能力保留在正式模型、令牌和 ability 范围。

## 已确认基线

- Windows、GitHub 跟踪分支与 FNOS checkout 当前为 clean
  `main@3ca372764023ac6088f0fad0e001dd7db87c8ea3`。
- 生产 New API 当前使用官方 `v1.0.0-rc.21`，镜像固定为
  `calciumion/new-api@sha256:428018a37c0b26c163a3367c18401161707cd0e08d0f26a3dde9ff0caa05e34c`。
- 目标使用官方 `v1.0.0-rc.23` manifest digest：
  `calciumion/new-api@sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695`。
- 目标镜像的 Linux AMD64 子镜像 digest 为
  `sha256:3811fa4be0f4ba2ab06651de3b6818cb52c4afa7eb04a467d63492cbb5f0830c`。
- rc.21 到 rc.23 跨越 71 个上游提交；涉及认证 Session、AuthFlow、渠道、
  Token、模型、relaykit 和数据库自动迁移。升级不能只验证容器 running。
- 当前 SQLite `PRAGMA integrity_check` 为 `ok`，共有 31 张表；生产渠道基座为
  `http://aurora:8080`，New API 当前正式范围只公开两个聊天模型。

官方依据：

- <https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.23>
- <https://github.com/QuantumNous/new-api/compare/v1.0.0-rc.21...v1.0.0-rc.23>
- <https://hub.docker.com/r/calciumion/new-api/tags>

New API 使用 AGPL-3.0 并带上游附加许可条款；本项目不修改或重新分发其上游
源码，只部署官方镜像并保留上游品牌和来源。

## 升级架构

本次不创建 New API canary，直接升级唯一生产实例，但必须在停止 New API 后先
生成冷备份。Aurora、Mihomo、MetaCubeXD、n8n、SMTP、PostgreSQL、cron 和
其他工作流保持运行且不修改。

执行顺序：

1. 本地只把 New API digest、版本注释、对应测试和四份正式文档更新到 rc.23；
   完成本地 Compose、测试、链接、ignore 和 Git 检查。
2. 经独立 Git 门禁提交、push，并用限定 Git bundle 快进 FNOS checkout。
3. FNOS 只拉取目标 digest，核对 RepoDigest、Linux AMD64、OCI 来源、版本、
   revision 和许可证标签；不使用 `latest`。
4. 停止唯一生产 `new-api`，冻结 SQLite 写入；创建权限 `0700` 的临时升级备份，
   其中数据库、数据文件和清单均为 `0600`。备份必须通过 SHA-256、SQLite
   `integrity_check`、渠道快照和文件清单校验。
5. 仅用 `docker compose up -d --no-deps --force-recreate new-api` 重建 New API。
   不重建 Aurora、Mihomo、MetaCubeXD 或 Studio OS 组件。
6. 等待启动和自动迁移完成，验证数据库、容器、登录、渠道、令牌与核心聊天；
   通过后再执行多模态矩阵。

## 浏览器控制边界

- 使用用户已授权的外部浏览器扩展和既有登录态查看 New API 控制台。
- 浏览器可以检查版本、渠道、模型、令牌范围、ability 和执行渠道测试，也可以
  为本次验收临时启用模型或 ability。
- 不读取、复制、输出或截图密码、客户端 Token、渠道密钥、Cookie、Session
  存储、ChatGPT Token 或响应正文。
- 如果 rc.23 的认证迁移使既有登录态失效，停止在登录页并请用户手工登录；
  不读取浏览器密码库或绕过身份验证。
- 控制台“测试成功”只能作为辅助证据；真实 OpenAI 兼容端点返回有效载荷才算
  能力 PASS。

## 生产升级门禁

升级后必须全部满足：

- `new-api` 为 rc.23 固定 digest、`aurora-stack/new-api` 标签、原工作目录、原
  `/data` 读写挂载、原端口和原重启策略，运行且重启计数为 0。
- SQLite `integrity_check` 通过；升级前 31 张表得到保留，新增迁移表允许增加，
  但不得丢失现有用户、渠道、Token、模型或 ability 记录。
- 管理控制台可登录，版本显示 rc.23，唯一 Aurora 渠道仍 active，基座严格为
  `http://aurora:8080`，服务密钥只做布尔一致性检查。
- 鉴权后的模型范围至少保留升级前两个聊天模型；`gpt-5-6-pro` 和
  `gpt-5-6-thinking` 各完成一次脱敏真实请求，至少一个返回结构合法的非空
  completion，另一个不得出现鉴权、路由或 Schema 失败。
- Aurora、Mihomo、MetaCubeXD、n8n 和 PostgreSQL 的 image ID、运行状态、
  重启计数、标签和挂载与升级前一致。

任一生产升级门禁失败，都停止 rc.23，恢复 rc.21 digest 和完整冷备份，再只重建
New API。恢复后必须重新验证数据库、渠道、登录和核心聊天；不得在失败状态继续
多模态测试。

## New API 多模态矩阵

所有请求都使用合成、非敏感、大小受限的输入，并从 New API 客户端端口进入；
不得用 Aurora 直连结果代替 New API 端到端结果。只输出 HTTP 状态、结构、媒体
类型、尺寸、时长、codec、语义标记和 PASS/FAIL，不输出图片、音频、base64、
签名 URL、提示词改写、转写正文或翻译正文。

矩阵包括：

1. `gpt-4o` 非流式与流式聊天。
2. `gpt-4o` 内联 PNG 图片理解。
3. Responses 非流式与流式。
4. Files 上传及后续引用。
5. `gpt-image-2` 图片生成。
6. 图片编辑。
7. 图片变体。
8. MP3 TTS，并用 FNOS 已有 `ffprobe` 从 stdin 验证 codec、采样率、声道和时长。
9. 英文 WAV 语音转写。
10. 原生 `/v1/audio/translations` 英译能力。
11. 英文转写后由 Chat 翻译成简体中文的组合链路。

图片或音频能力只有返回可解码的真实媒体数据才算 PASS。原生音频翻译只有返回
英文才算符合 OpenAI 兼容语义；返回原语言或中文均为 FAIL。

## 失败调查与保留策略

多模态单项失败时，先按以下顺序定位，不立即把失败归因于 New API：

1. New API Token 模型限制、渠道模型、ability、分组和路由选择。
2. rc.23 relay 请求转换、multipart 字段、响应结构和媒体大小限制。
3. New API 是否把请求实际转发到 `http://aurora:8080`。
4. Aurora 本地路由、账号选择、Sentinel、ChatGPT Web 上游和 Mihomo 新加坡出口。

调查只输出脱敏分类和阶段状态。允许在 New API 控制台修正本次升级直接造成的
模型、Token、ability 或 relay 配置问题；不得更换 Aurora 镜像、修改代理订阅、
读取凭据正文或新增补偿服务。

修复后每个失败能力最多执行一次针对性真实复验。仍失败、只部分通过或返回无效
媒体的能力，必须从正式渠道模型、默认 Token 范围和 ability 中隐藏。核心聊天
正常时，多模态单项失败不回退 rc.23。

## 验收与清理

- 升级完成后重新发布脱敏 `latest.json`，要求总体 PASS；n8n 专用子挂载继续
  `RW=false`，n8n 工作流、SMTP 和 PostgreSQL 不修改。
- 运行项目测试、Compose 静态解析、Markdown 链接、`git diff --check`、
  `git show-ref`、`git fsck --full` 和单一 worktree 检查。
- 临时升级备份和 rc.21 镜像在最终验收后继续保留；删除必须另行获得用户明确
  授权。多模态测试产生的临时媒体不得落盘；确需短暂落盘的文件必须位于受控
  临时目录并在验收结束后核对精确路径再删除。

## 非目标

- 不把 New API 迁移到 PostgreSQL 或 Redis。
- 不修改 Aurora、Mihomo、MetaCubeXD、n8n、SMTP、PostgreSQL 或 cron。
- 不新增外部 Token 换新、容器重启补偿、监控服务或长期 canary。
- 不因为 rc.23 支持更多上游渠道而新增其他供应商、Credential 或计费配置。
