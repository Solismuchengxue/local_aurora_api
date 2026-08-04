# 飞牛 fnOS 部署指南：aurora + new-api 反向代理 ChatGPT Web 为 OpenAI 兼容 API

> 本文档记录唯一正式部署。2026-08-04 已完成 Aurora 2.5.0 Session Token 唯一生产切换和 New API rc.23 升级；旧 Aurora 与 canary 已清理，rc.21 镜像及已验证 New API 升级备份继续保留，等待独立清理授权。
> 目标：把 **ChatGPT Web** 转成通用 **OpenAI 兼容 API**，供任意客户端（OpenAI SDK、Cherry Studio、LobeChat 等）调用，主用模型 `gpt-5-6-pro`。
> 项目共享名称和 FNOS 当前运行目录均为 `Solis_Aurora_Gateway`；用户 cron 也已使用 `/vol1/1000/Solis_Aurora_Gateway`。历史目录 `/vol1/1000/local_aurora_api` 已在最终备份、观察和退役门禁通过后删除。
> 脱敏健康状态生产器、生产 cron、Studio OS 专用只读子挂载和 n8n 工作流导入已于 2026-08-02 完成；合成异常邮件与真实 `PASS` 静默路径均已验证，工作流随后通过最终门禁并正式发布、激活。

---

## 0. 架构总览

```
客户端 / OpenAI SDK
   │  base_url = http://<NAS_IP>:3000/v1
   ▼
new-api (:3000)          ← API 网关，管理客户端令牌、渠道和模型分发
   │  base_url = http://aurora:8080，密钥 = Aurora service key
   ▼
aurora (:8080)           ← ChatGPT Web → OpenAI 兼容反向代理（校验 service key）
   │  http_proxy = http://mihomo:7890   （仅 aurora 走代理）
   ▼
mihomo (:7890)           ← Clash.Meta 内核，GLOBAL 锁定 🇸🇬 新加坡 GPT 解锁节点
   │
   ▼
ChatGPT (api.openai.com / chatgpt.com)

.secrets/session_tokens.txt ──只读挂载──▶ Aurora 内部换取/续期 Access Token
```

辅助组件：
- **metacubexd** (:9097)：mihomo 官方开源 Web 面板，可视化管理节点/分组/流量。
- **watchcow**（飞牛应用）：把 new-api / metacubexd 变成飞牛桌面图标（可选增强，推荐 GUI 手动配置）。

**关键特性**：mihomo 是**应用级代理**（无 TUN 模式）；aurora 通过 Compose 网络显式使用它，Docker daemon 另通过宿主机 `127.0.0.1:7897` 回环映射只代理镜像仓库操作。它不接管飞牛系统和 IPv6 DDNS 流量。

---

## 1. 前置条件

| 项目 | 说明 |
|---|---|
| 飞牛 fnOS | 已安装 Docker，当前用户在 docker 组 |
| ChatGPT session token | 写入受保护的 Session Token 文件，由 Aurora 内部换取和自然续期 Access Token |
| Aurora service key | 写入 `.env` 的 `AURORA_AUTHORIZATION`，并作为 New API 渠道密钥 |
| mihomo 订阅 | 机场**订阅链接**，需包含 **新加坡 GPT 解锁节点**；经 mihomo WebUI 导入 |
| 访问方式 | 能用 SSH 登录 NAS（密钥或密码），或用飞牛内置终端 |

> ⚠️ **Token 安全**：Session Token 等同 ChatGPT 网页登录态，不要外泄、不要写进文档、镜像、日志或 Git。

---

## 2. 配置 Docker 镜像源（部署前必须先解决）

部署前必须先确保飞牛 Docker 能拉取本栈镜像：`ghcr.io/aurora-develop/aurora`、`ghcr.io/metacubex/metacubexd`、`metacubex/mihomo`、`calciumion/new-api`。

- **本环境已配置**：飞牛 daemon 已配 `registry-mirrors`（如 `docker.fnnas.com`），`ghcr.io` 与 `metacubex/mihomo` 均可直连，**可跳过本节**。
- **全新部署 —— 二选一**：
  1. **镜像加速地址**：飞牛 **Docker 设置 → 镜像加速** 填入可用加速地址（或编辑 `/etc/docker/daemon.json` 的 `registry-mirrors`），重启 Docker 后生效。
  2. **局域网代理**：把 Docker 的镜像源 / HTTP 代理指向局域网内的 Clash（如 `http://<LAN_PROXY_IP>:<PORT>`），让镜像拉取经局域网代理出网。
- 上述两种方式的代理**都只影响拉镜像，不影响运行时流量**。

> ⚠️ 镜像源 / 代理仅用于拉取，**不要当作运行时出网方式**。本栈的 ChatGPT 出网走的是 mihomo（见 §0），与镜像源无关。

仓库中的 Compose 不使用会随上游发布漂移的 `latest`。Aurora、Mihomo、MetaCubeXD 和 New API 均使用 NAS 已验证的不可变 digest；New API 正式基线为官方 `v1.0.0-rc.23` manifest `sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695`。升级时一次只修改一个服务的 digest；先备份其完整持久化数据，再执行拉取、重建和完整验证。

---

## 3. 目录结构

把本项目复制到飞牛数据卷，例如 `/vol1/YOUR_USER_ID/Solis_Aurora_Gateway/`：

```
/vol1/YOUR_USER_ID/Solis_Aurora_Gateway/
├── .env                         # SESSION_SECRET、AURORA_AUTHORIZATION、NAS_LAN_IP，不提交
├── .secrets/
│   └── session_tokens.txt       # 权限 600，归属 65532:65532，不提交
├── docker-compose.yml
├── config/
│   └── mihomo/
│       └── config.example.yaml # 首次启动示例
├── backups/                    # 当前正式栈恢复包，正文不提交
└── data/
    ├── mihomo/
    │   └── config.yaml         # 运行配置，会被订阅或 WebUI 更新
    └── new-api/                # New API 持久化数据
```

首次部署时初始化本地文件：

```bash
cd /vol1/YOUR_USER_ID/Solis_Aurora_Gateway
cp .env.example .env
mkdir -p data/mihomo data/new-api .secrets
install -m 600 /dev/null .secrets/session_tokens.txt
cp config/mihomo/config.example.yaml data/mihomo/config.yaml
```

编辑 `.env`：

- 把 `SESSION_SECRET` 设为 `openssl rand -hex 16` 生成的随机值。
- 把 `AURORA_AUTHORIZATION` 设为另一枚独立随机服务密钥；New API 渠道密钥必须使用同一值。
- 把 `NAS_LAN_IP` 设为 NAS 的局域网 IPv4 地址，例如 `192.168.0.38`。该值用于限制 Mihomo 控制端口的监听网卡。

把 Session Token 写入 `.secrets/session_tokens.txt`，然后设置归属与权限：

```bash
chown 65532:65532 .secrets/session_tokens.txt
chmod 600 .secrets/session_tokens.txt
```

只在首次部署时复制 Mihomo 示例配置；已有 `data/mihomo/config.yaml` 时不要覆盖。

> 本栈只挂载一个 Session Token 文件，不挂载 `access_tokens.txt`，也不允许 New API 把 ChatGPT Token 当作渠道密钥。包含数据库、配置或凭据的归档只放在被 Git 忽略的 `backups/`；项目级备份必须排除该目录，避免递归打包。

---

## 4. docker-compose.yml

项目根目录的 [`docker-compose.yml`](../docker-compose.yml) 是唯一 Compose 配置入口，不在本文重复维护副本。

其中：

- Mihomo 和 New API 的可变数据统一写入被 Git 忽略的 `data/`。
- Mihomo 的 `7890` 只在 Compose 网络内提供给 Aurora；`9090` 仅绑定 `NAS_LAN_IP`。
- Aurora 显式设置 `PROXY_URL`、`http_proxy`、`Authorization` 和 `ENABLE_EXTERNAL_TOKEN=false`，并以 `65532:65532` 读取只读 Session Token 挂载。
- `SESSION_SECRET`、`AURORA_AUTHORIZATION` 与 `NAS_LAN_IP` 必须从本地 `.env` 提供；缺失时 Compose 会直接报错。

> 镜像拉取：镜像源配置见 §2，飞牛 daemon 已配 `registry-mirrors`（docker.fnnas.com），`ghcr.io` 与 `metacubex/mihomo` 均可直连。

---

## 5. 启动服务

在 `/vol1/YOUR_USER_ID/Solis_Aurora_Gateway/` 目录执行（镜像源已在 §2 确认可用）：

```bash
cd /vol1/YOUR_USER_ID/Solis_Aurora_Gateway
docker compose up -d
```

**验证**：
```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```
应看到 `mihomo` / `aurora` / `new-api` / `metacubexd` 均为 `Up`。

> ⏳ aurora 启动较慢（约 2 分钟才监听 8080），属正常。

---

## 6. 配置 mihomo（订阅 + WebUI）

mihomo 的节点和规则**通过机场订阅导入**，日常管理在 **mihomo WebUI（即 metacubexd 面板 :9097）** 完成，不需要手工编辑 `config.yaml`。

### 6.1 关键设置（必须满足，否则 aurora 解锁失败）

在 mihomo WebUI 中确认 / 设置：

1. **不要开 TUN 模式**——保持应用级代理，飞牛系统 / IPv6 DDNS 公网访问不受影响。
2. **出站模式设为 `GLOBAL`**——所有经过 mihomo 的流量统一走 GLOBAL 节点组，绕过规则。
3. **GLOBAL 组首选节点必须是新加坡 GPT 解锁节点**——这是 aurora 出口节点的唯一决定因素。
4. 容器内的 `external-controller` 开在 `0.0.0.0:9090`；Compose 只把它发布到 `NAS_LAN_IP:9090`，供局域网浏览器中的 MetaCubeXD 连接。

> ⚠️ **切勿把出站模式改成 `rule`**：规则里通常会把 `chatgpt.com`/`openai.com` 指向机场主组中的其他地区节点，切到 rule 模式会让 aurora 的 ChatGPT 请求改走非新加坡节点，可能解锁失败。

### 6.2 节点导入方式（订阅 + WebUI）

1. 从机场拿到 **订阅链接**（输出格式选 mihomo / Clash.Meta）。
2. 在 **mihomo WebUI**（即 metacubexd 面板 `:9097`，连接 mihomo 的 external-controller）：
   - 进入 **配置 → 核心配置**
   - 在 **Actions** 区输入你的**订阅链接**
   - 点击 **拉取远程配置**（Update）

   节点列表与 GLOBAL 等分组会自动生成。
3. 将 GLOBAL 组首选节点设为新加坡解锁节点（见 6.1 第 3 点）。

> mihomo 容器需先有 `data/mihomo/config.yaml` 才能启动（目录见 §3）；项目提供的示例只负责首次启动。导入订阅后，节点与规则以运行配置为准。每次更新订阅后都应重新确认 `mode: GLOBAL` 和 GLOBAL 首选节点，不要假设订阅更新一定保留选择。

---

## 7. 初始化 new-api（关键坑）

New API 镜像**首次启动是未初始化状态**，默认 `root/123456` 登录会失败。

1. 浏览器打开 `http://<NAS_IP>:3000`
2. 会自动跳到**初始化页面** → 设置管理员账号、密码、邮箱
3. 用刚设的账号登录

> 如果之前装过、数据库已初始化但忘了密码：备份并清库重建——
> ```bash
> cd /vol1/YOUR_USER_ID/Solis_Aurora_Gateway
> mv data/new-api/one-api.db data/new-api/one-api.db.bak-$(date +%Y%m%d-%H%M%S)
> docker compose up -d --force-recreate new-api
> ```
> 重启后日志出现 `system is not initialized and no root user exists` 即表示回到首次初始化状态。

---

## 8. 配置 aurora 渠道

登录 new-api 后：

1. 左侧 **渠道管理** → **添加渠道**
2. 填写：
   - 类型：**OpenAI**
   - 名称：`aurora`（随意）
   - 基座地址：`http://aurora:8080`（compose 网络内用容器名）
   - 密钥：填写 `.env` 中 `AURORA_AUTHORIZATION` 的同一服务密钥
   - 模型：填 `*` （或留空，后续从上游获取）
3. 保存

> New API 只持有 Aurora service key；ChatGPT Session Token 只存在于受保护的只读挂载中。两者用途不同，不得互换或输出。

### ⚠️ 必须做的一步：从上游获取模型

new-api `v1.0.0-rc.21` 引入了**分发器（distributor）**机制——具体模型名必须在 `models` 表注册，否则只能 `model=*` 兜底，指定模型会报 `model_not_found`。

1. 在渠道列表找到刚建的 `aurora` 渠道 → 点 **「从上游获取」** 按钮
2. 等待同步完成（会拉取 Aurora 暴露的模型列表，至少确认 `gpt-5-6-pro` 和 `gpt-5-6-thinking`）
3. 保存

**验证**（用 new-api 的「令牌」+ 任意 OpenAI 客户端或 curl）：
```bash
curl http://NAS_IP:3000/v1/chat/completions \
  -H "Authorization: Bearer <new-api令牌>" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-6-pro","messages":[{"role":"user","content":"ping"}]}'
```
返回 200 且 `choices` 有内容即成功。

---

## 9. 客户端配置

在任意 OpenAI 兼容客户端（Cherry Studio / LobeChat / 代码 SDK）填：

| 字段 | 值 |
|---|---|
| Base URL | `http://<NAS_IP>:3000/v1` |
| API Key | new-api 里生成的**令牌**（不是 ChatGPT token） |
| Model | `gpt-5-6-pro`（日常）或 `gpt-5-6-thinking`（复杂任务） |

> 令牌获取：new-api 左侧 **令牌** → 创建令牌 → 复制 `sk-...`。
>
> WorkBuddy 的 `vendor: "Custom"` 会自动追加 `/chat/completions`，因此 URL 必须填到 `/v1`；当前 Aurora 构建还要求两个模型都设置 `supportsToolCall: false`。完整配置和“？”排障见 [WorkBuddy 自定义模型踩坑指南](workbuddy_custom_models.md)。

### 9.1 活端点能力实测

#### New API rc.23 升级与多模态门禁

2026-08-04 已把唯一生产 New API 从 rc.21 升级到 rc.23。升级前停止 New API 并
冷备份整个 `data/new-api/`，随后完成 SQLite 迁移、登录、渠道、核心聊天和容器门禁；
未触发回退。rc.21 镜像与完整冷备份继续保留，后续清理需要独立授权。

多模态验收曾临时把 `gpt-4o`、`gpt-image-2`、`tts-1`、`whisper-1` 加入渠道、
默认 Token 和 ability，并从 FNOS 仅执行一次以下零重试矩阵：

```bash
python3 scripts/new_api_multimodal_probe.py \
  --root /vol1/1000/Solis_Aurora_Gateway \
  --allow-real-api \
  --output /tmp/new-api-multimodal-report.json \
  --json
```

脚本只连接固定的 `http://127.0.0.1:3000`，从 SQLite 在内存选择一枚可用客户端
Token，并使用合成、非敏感、大小受限的图片和音频输入。报告不含 Token、提示词、
响应正文、图片、音频、base64、签名 URL 或原始错误。图片、TTS、语音和组合翻译
必须返回有效结构或可解码媒体才算 PASS；原生音频翻译未返回英文时继续隐藏。

实际结果为 8 PASS、6 FAIL。模型列表、非流式 Chat、非流式 Responses、视觉、图片
生成、图片编辑、音频转写和原生翻译为英文通过；Chat/Responses 流式、Files、图片
变体、TTS 媒体和英文音频转中文组合链路失败。按 passed-only 规则，仅保留完整通过
的 `whisper-1`，最终渠道、默认 Token 和 abilities 严格等于 `gpt-5-6-pro`、
`gpt-5-6-thinking`、`whisper-1`。临时报告已校验后删除。

以下结论来自 2026-07-26 使用 curl/Python 对 NAS 活端点的实际调用。它们描述的是当时运行中的 Aurora 构建，而不是根据上游文档推断。

#### 思考强度：切模型，不传 effort 参数

- `reasoning_effort` 和 Responses API 的 `reasoning.effort` 均返回 `422 Invalid conversation body`。
- 日常快速调用使用 `gpt-5-6-pro`。
- 复杂分析、代码和规划使用 `gpt-5-6-thinking`。
- `gpt-5-6-thinking` 的复杂问题表现更稳，但返回的 `reasoning_content` 为空；第三方客户端看不到思考过程。

因此，当前正式用法是切换模型，不在 New API 参数覆盖或请求体中强塞 effort 参数。

#### 图片生成：生产入口通过部分能力，但正式隐藏

- 2026-08-04 经 rc.23 生产 New API 入口，`gpt-image-2` 图片生成和编辑返回了可解码图片，但图片变体在进入渠道前被模型范围拒绝。
- passed-only 门禁按模型而不是按单个端点收敛，因此当前正式配置已从渠道模型列表、默认令牌模型范围和 abilities 中隐藏 `gpt-image-2`；鉴权后的 `GET /v1/models` 只应列出 pro、thinking、whisper 三项。
- New API 的“测试全部模型”可以把 `gpt-image-2` 显示为成功并给出响应时间；这只说明渠道测试完成，不能证明返回了图片。
- 2026-07-27 的旧 Aurora 曾在图片生成 sentinel 准备阶段失败；该历史现象已被后续生成/编辑成功取代，但不能覆盖 2026-08-04 图片变体仍失败的事实。

当前部署不得把图片生成功能标记为正式可用。未来只有在图片生成、编辑和变体全部通过新的受控门禁后，才可重新保留该模型。WorkBuddy 的 `supportsImages` 表示图片输入/看图，不代表该端点能够生成图片。

#### 音频：转写与翻译为英文可用

- `whisper-1` 的 `/v1/audio/transcriptions` 已返回合法转写结构。
- `whisper-1` 的 `/v1/audio/translations` 已返回稳定英文标记；该 OpenAI 兼容端点的语义是把音频翻译成英文，不接受“目标语言为中文”的参数。
- `tts-1` 虽到达 Aurora 渠道，但返回媒体未通过 MP3 codec、采样率、声道和时长验收，因此继续隐藏。
- 英文音频先转写再交给 `gpt-4o` 翻译为中文的组合链路发生语义不匹配；`gpt-4o` 同时还有流式和 Files 失败，故未纳入正式模型范围。

#### 网页搜索：模型原生联网可用

- `gpt-5-6-pro` 和 `gpt-5-6-thinking` 都能自动触发 ChatGPT 原生联网搜索。
- 客户端不需要另外提供搜索工具；联网行为由 ChatGPT Web 上游完成。
- 响应可能包含 `turn0searchN` 等引用标记，偶有乱码或截断。
- `gpt-5-6-pro` 偶发 500 时可有限重试，但不能把重试掩盖为稳定成功。
- WorkBuddy 通常优先使用自身的 WebSearch 工具，一般不依赖这项模型原生搜索能力。

#### 深度研究：不纳入正式架构

Aurora 没有 ChatGPT Deep Research 的一键端点。由于使用频率很低，该能力不纳入正式架构，项目不提供或维护相关脚本。

---

## 10. 飞牛应用化（watchcow，可选）

把 new-api / metacubexd 变成飞牛桌面图标，点一下跳转。

### ⚠️ 版本坑（实测）

- 飞牛应用中心**在线版 0.3.3 无法生成应用**（不请求 Docker 套接字权限，watchcow 收不到容器事件）。
- **正确做法**：GitHub release 下载 **0.4.4** 的 `watchcow-x86.fpk`，手动安装（安装时无需额外弹窗，会自动获得 docker 事件权限）。

### 步骤

1. 从 <https://github.com/tf4fun/watchcow/releases/tag/v0.4.4> 下载 x86 安装包
2. 放到飞牛本地目录；当前工作副本保存在 `artifacts/watchcow-0.4.4-x86_64.fpk`
3. 飞牛 **应用中心** → **本地安装 / 手动安装** → 选择该 fpk → 完成安装

### 推荐：watchcow GUI 手动配置

安装 0.4.4 后，在 watchcow 界面会看到 `new-api` / `metacubexd` 等容器（未配置状态），**手动添加应用**即可：

**New API（new-api 容器）**
- 显示名：New API
- 端口：`3000` ｜ 协议：http ｜ 路径：`/`
- UI 类型：url（新标签页打开）或 iframe（内嵌）
- 图标（可选）：项目中的 `assets/icons/newapi.png`，或自行提供可访问的 URL
- 描述：ChatGPT 统一 API 网关

**Mihomo 面板（metacubexd 容器）**
- 显示名：Mihomo 面板
- 端口：`9097` ｜ 协议：http ｜ 路径：`/`
- UI 类型：url
- 图标（可选）：项目中的 `assets/icons/clash.png`，或自行提供可访问的 URL
- 描述：Clash 代理控制面板

> 手动模式的代价：容器重建后不会自动恢复这两个应用，需重新手动添加。日常想改图标 / 名称，在 GUI 里直接改即可，比标签模式更灵活。

### 备选：compose 标签自动（不推荐，但可自动同步）

若希望容器重建自动恢复应用，可在 compose 的 `new-api` / `dashboard` 服务加 labels，再 `docker compose up -d --force-recreate`（labels 不可热更新）：

```yaml
  new-api:
    labels:
      watchcow.enable: "true"
      watchcow.display_name: "New API"
      watchcow.service_port: "3000"
      watchcow.protocol: "http"
      watchcow.path: "/"
      watchcow.ui_type: "url"
      watchcow.icon: "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/chatgpt.png"

  dashboard:
    labels:
      watchcow.enable: "true"
      watchcow.display_name: "Mihomo 面板"
      watchcow.service_port: "9097"
      watchcow.protocol: "http"
      watchcow.path: "/"
      watchcow.ui_type: "url"
      watchcow.icon: "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/wireguard.png"
```

> 标签模式会让 watchcow 接管配置、GUI 内不可手动编辑；且标签变更需删容器重建。日常维护更推荐上面的 GUI 手动方式。

---

## 11. 维护与排障

### 11.1 Session Token 内建自然续期

唯一正式 Aurora 只读挂载 `.secrets/session_tokens.txt`。Aurora 2.5.0 在启动时用 Session Token 换取 Access Token，并由内部后台健康检查续期已过期的 Session/Refresh 账号。

- New API 渠道密钥始终是 `AURORA_AUTHORIZATION`，不会因 ChatGPT Access Token 到期而改变。
- 不安装旧外部换新脚本，不保留 04:17/16:17 外部换新 cron、刷新日志或旧 Token 回退副本。
- Aurora 无法自愈时由 05:12/17:12 健康状态和 05:15/17:15 n8n 告警通知；不得增加补偿换新脚本或自动重建，用户手工更新 Session Token 后再按受控门禁重建唯一正式 Aurora。
- Session Token 文件必须保持 `600` 和 `65532:65532`，不得通过放宽权限解决读取失败。

### 11.2 一键只读健康检查

在项目根目录运行：

```bash
python3 scripts/check_stack_health.py
python3 scripts/check_stack_health.py --json
python3 scripts/check_stack_health.py \
  --root /path/to/Solis_Aurora_Gateway \
  --channel-id 1 \
  --json
```

`--root` 指定包含 `data/` 与 `.secrets/` 的项目根目录，默认使用脚本所在项目；
`--channel-id` 指定要检查的已启用 Aurora 渠道 ID，默认值为 `1`。

精简 JSON 输出示例（仅含安全的示意状态字段，不包含 Token、响应正文或错误原文）：

```json
{
  "checked_at": "2026-07-29T12:00:00+08:00",
  "overall": "PASS",
  "checks": [
    {"name": "containers", "status": "PASS", "summary": "4/4 运行，重启次数均为 0", "details": {"running": 4}},
    {"name": "database", "status": "PASS", "summary": "数据库完整，正式 Aurora 渠道与服务密钥一致", "details": {"integrity": "ok", "channel_base_matches": true, "service_key_matches": true}},
    {"name": "refresh_log", "status": "PASS", "summary": "Access Token 由 Aurora 内部自然续期，外部刷新日志不适用", "details": {"mode": "aurora_internal", "external_refresh": "not_applicable"}},
    {"name": "mihomo", "status": "PASS", "summary": "GLOBAL / SG / Singapore Node", "details": {"mode": "GLOBAL", "selected": "Singapore Node", "country": "SG"}},
    {"name": "models", "status": "PASS", "summary": "模型范围严格等于 pro、thinking、whisper", "details": {"model_ids": ["gpt-5-6-pro", "gpt-5-6-thinking", "whisper-1"]}},
    {"name": "chat", "status": "PASS", "summary": "pro 返回结构合法的非空 completion", "details": {"model": "gpt-5-6-pro", "content_empty": false, "fallback_used": false}}
  ]
}
```

检查范围包括：

- 四个生产容器均为 running 且重启次数为 0；
- New API SQLite 完整性、正式渠道地址、服务密钥一致性和可用客户端令牌；
- Aurora 内建自然续期模式，外部刷新日志固定为不适用；
- Mihomo GLOBAL 模式、当前节点和经代理确认的 `SG` 出口；
- 对外模型严格等于 `gpt-5-6-pro`、`gpt-5-6-thinking`、`whisper-1`；
- 一次真实 New API 聊天请求及 OpenAI completion 结构。

只有通过或警告时退出码为 `0`；任一失败项使退出码为 `1`。合法 HTTP 200 空 completion 会显示警告，但不会被误判为鉴权失败。

脚本不会修改数据库、Token、容器、节点选择或配置，也不会输出凭据和聊天正文。

### 11.3 n8n 离线健康状态生产器（已部署并激活）

`scripts/write_n8n_health_status.py` 是与完整健康检查隔离的生产器。它只读取限定的 Docker 元数据、本地 TCP、SQLite 正式渠道地址与服务密钥一致性，并发布固定的“外部刷新不适用”状态，原子替换一个固定 Schema v1 的脱敏 `latest.json`；不调用模型、聊天、代理出口或外部 API。

2026-08-01 的 FNOS 只读预检确认：Mihomo 容器桥接地址的 `9090` 可达，但 FNOS 主机回环访问其指定 LAN 发布地址超时。生产器因此仍先验证 `docker port` 返回合法发布绑定，再仅对 Mihomo 使用限定 `docker inspect` 动态取得容器桥接 IPv4 做 TCP 建连；其余三个服务继续检查本机发布地址。任何实际地址都不会写入状态文件。

FNOS 生产命令：

```bash
python3 scripts/write_n8n_health_status.py \
  --root /vol1/1000/Solis_Aurora_Gateway \
  --output /vol1/1000/Solis_Studio_OS/data/ops/aurora-gateway/latest.json \
  --channel-id 1
```

生产器不创建输出父目录；父目录缺失、为符号链接或权限不正确时失败关闭。活动目录只允许滚动的 `latest.json`，文件最大 16 KiB，不生成逐次状态或日志历史。退出码如下：

- `0`：已发布 `PASS` 或 `WARN`；
- `1`：已发布 `FAIL`；
- `2`：生产器自身错误，未发布新状态。

已安装的用户 cron 使用 `Asia/Shanghai` 的 05:12 和 17:12；外部 Token 换新 cron 已退役：

```cron
12 5,17 * * * cd /vol1/1000/Solis_Aurora_Gateway && /usr/bin/python3 scripts/write_n8n_health_status.py --root /vol1/1000/Solis_Aurora_Gateway --output /vol1/1000/Solis_Studio_OS/data/ops/aurora-gateway/latest.json --channel-id 1 >/dev/null 2>&1
```

2026-08-02 已完成以下部署与验证：

1. FNOS 现场核对 Python、Docker 限定字段、本地端口、SQLite/续期元数据、目录权限和时区；
2. 创建受管状态目录并安装上述 cron；活动目录仅保留一个滚动 `latest.json`；
3. 保留 `/exchange` 读写父挂载，同时把 `/exchange/ops/aurora-gateway` 覆盖为专用只读子挂载；`docker inspect` 显示子挂载 `RW=false`，容器内写入哨兵因只读文件系统失败且宿主机无残留，父目录的独立读写验证不受影响；
4. 导入 `Solis Aurora Gateway Alert (Phase 1)` 时保持 `active=false`，完成后续门禁再发布、激活；工作流时区为 `Asia/Shanghai`，计划在 05:15、17:15 检查；
5. 手工合成异常执行成功并确认现有 SMTP 通知送达；随后手工刷新合法 `PASS` 状态，从计划触发器运行真实文件读取链，执行成功且未进入邮件节点。两次验证均未调用真实 Aurora API。

最终门禁重新确认 producer cron、单一状态文件、专用只读子挂载、容器健康、工作流计划与 `active=false` 后，仅发布并激活目标工作流。激活后数据库状态为 `active=true`，活动版本与当前版本一致，计划触发器已注册；目标执行数保持为两次验证执行，没有因激活产生手工执行，也没有修改 SMTP、数据库、Compose、cron、其他工作流或 Credential。

Aurora 是状态权威，Studio OS/n8n 只能消费，不能通过该链路修改 Aurora。文件型状态生产、挂载、读取和校验不新增或复用 Credential；现有 SMTP Credential 只属于异常通知。`latest.json` 会随 Studio OS `data/` 进入本地恢复包和加密云备份，因此内容不得包含 Token、连接串、邮箱、Cookie、敏感路径、业务正文、原始日志/命令输出，也不得扩展为无界历史。

2026-08-04 在 New API 模型范围最终收敛后再次手工运行生产器，Schema v1 的 containers、runtime_contract、local_tcp、database、refresh_log 五项均为 `PASS`。发布后的单一 `latest.json` 保持 0600 和 16 KiB 上限，n8n 专用子挂载继续为 `RW=false`；本次没有执行模型请求、工作流或邮件节点。

### 11.4 mihomo 保持 GLOBAL 模式

在 Mihomo WebUI 中确认出站模式为 `GLOBAL`、GLOBAL 首选节点为新加坡解锁节点（见 §6.1）。切换到 `rule` 可能让 Aurora 改走其他节点。每次更新订阅后重新确认模式和节点选择。

### 11.5 不影响飞牛 IPv6 DDNS

当前设计不启用 Mihomo TUN；Aurora 仅配置应用级 `http_proxy`，Docker daemon 仅配置镜像仓库代理，因此不会主动接管飞牛系统路由。部署后仍应实际验证 IPv6 DDNS 和防火墙行为。

### 11.6 Mihomo 端口边界

Mihomo 的 `7890` 代理端口不向局域网发布：Compose 网络内的 Aurora 直接使用，Docker daemon 只通过宿主机 `127.0.0.1:7897` 回环映射使用。`9090` 控制端口没有默认认证，但只绑定 `.env` 中的 `NAS_LAN_IP`，不会监听 NAS 的 IPv6 地址或其他 IPv4 网卡。

这项绑定不能替代边界防火墙：仍应确认路由器没有把 `9090` 转发到公网，并避免把 `NAS_LAN_IP` 设置成 `0.0.0.0`。

### 11.7 排障速查

| 现象 | 原因 | 解决 |
|---|---|---|
| 镜像拉取失败 / 超时 | Docker 镜像源不可用 | 先解决镜像源（§2），再 `docker compose up -d` |
| aurora 报 `no available account` | token 失效 / 渠道密钥填错 | 在 new-api 渠道重新填入有效 token（§8），`docker compose restart aurora` |
| new-api `root/123456` 登不上 | 镜像未初始化或密码已改 | 清库重建（§7） |
| 指定模型 `model_not_found` | rc.21 及后续分发器未注册模型 | 渠道点「从上游获取」同步模型（§8） |
| WorkBuddy 只显示“？” | URL 缺少 `/v1`、启用了工具调用或旧会话被拒绝历史污染 | 核对 [WorkBuddy 指南](workbuddy_custom_models.md)，并先新建对话测试 |
| ChatGPT 返回解锁/地区错误 | aurora 走了非新加坡节点 | 确认出站模式 `GLOBAL` + GLOBAL 首选=新加坡（§6.1） |
| watchcow 装不出应用 | 应用中心 0.3.3 无 docker 权限 | 改装 GitHub 0.4.4 fpk（§10） |
| aurora 接口 502/超时 | aurora 未完全启动（约2分钟） | 等启动完成再测 |

---

## 12. 一键检查清单（部署后验证）

- [ ] Docker 镜像源可用（§2），`docker compose up -d` 四个镜像均拉取成功
- [ ] `docker ps` 四个容器均 Up
- [ ] 浏览器开 `http://<NAS_IP>:9097` 能看到 mihomo 面板、节点可选、出站模式为 GLOBAL
- [ ] new-api 初始化完成、能登录（§7）
- [ ] aurora 渠道已填入有效 token 并「从上游获取」模型（§8），测试 `gpt-5-6-pro` 返回 200
- [ ] 客户端 base_url / api_key / model 配置正确，能对话（§9）
- [ ] （可选）飞牛桌面出现 New API / Mihomo 面板图标（watchcow GUI 手动添加，§10）
- [ ] 确认出站模式 `GLOBAL` 且 GLOBAL 首选节点为新加坡解锁节点（§6.1）
- [ ] 确认主机未发布 `7890`，`9090` 仅监听 `NAS_LAN_IP`，且路由器没有把 `9090` 转发到公网
