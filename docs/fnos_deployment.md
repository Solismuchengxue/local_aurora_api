# 飞牛 fnOS 部署指南：aurora + new-api 反向代理 ChatGPT Web 为 OpenAI 兼容 API

> 本文档基于 2026-07-26 在飞牛 fnOS 上的实际部署验证整理；NAS 仍运行旧目录 `aurora-stack`。2026-07-27 已在独立的 `local_aurora_api` 工作副本中准备代码、凭据引用和运行数据副本，并通过 Compose 静态解析，但尚未从新目录启动或切换容器。
> 目标：把 **ChatGPT Web** 转成通用 **OpenAI 兼容 API**，供任意客户端（OpenAI SDK、Cherry Studio、LobeChat 等）调用，主用模型 `gpt-5-6-pro`。

---

## 0. 架构总览

```
客户端 / OpenAI SDK
   │  base_url = http://<NAS_IP>:3000/v1
   ▼
new-api (:3000)          ← API 网关，管理令牌、渠道、模型分发
   │  渠道(类型 OpenAI) base_url = http://aurora:8080，密钥 = ChatGPT access_token
   ▼
aurora (:8080)           ← ChatGPT Web → OpenAI 兼容反向代理（透传 Bearer token）
   │  http_proxy = http://mihomo:7890   （仅 aurora 走代理）
   ▼
mihomo (:7890)           ← Clash.Meta 内核，GLOBAL 锁定 🇸🇬 新加坡 GPT 解锁节点
   │
   ▼
ChatGPT (api.openai.com / chatgpt.com)
```

辅助组件：
- **metacubexd** (:9097)：mihomo 官方开源 Web 面板，可视化管理节点/分组/流量。
- **watchcow**（飞牛应用）：把 new-api / metacubexd 变成飞牛桌面图标（可选增强，推荐 GUI 手动配置）。

**关键特性**：mihomo 是**应用级代理**（无 TUN 模式），只有显式配 `http_proxy` 的 aurora 走它；按当前网络设计，它不接管飞牛系统和 IPv6 DDNS 流量。

---

## 1. 前置条件

| 项目 | 说明 |
|---|---|
| 飞牛 fnOS | 已安装 Docker，当前用户在 docker 组 |
| ChatGPT access_token | 形如 `eyJ...` 的 JWT；有效期有限，在 new-api 渠道密钥里填入（见 §8） |
| mihomo 订阅 | 机场**订阅链接**，需包含 **新加坡 GPT 解锁节点**；经 mihomo WebUI 导入 |
| 访问方式 | 能用 SSH 登录 NAS（密钥或密码），或用飞牛内置终端 |

> 🔑 **获取 ChatGPT access_token**：浏览器登录 ChatGPT 后，访问 <https://chatgpt.com/api/auth/session> ，在返回的 JSON 中找到 `accessToken` 字段（`eyJ...` 那串）即为 token。
>
> ⚠️ **token 安全**：token 等同 ChatGPT 网页登录态，不要外泄、不要写进会公开的文档/镜像。

---

## 2. 配置 Docker 镜像源（部署前必须先解决）

部署前必须先确保飞牛 Docker 能拉取本栈镜像：`ghcr.io/aurora-develop/aurora`、`ghcr.io/metacubex/metacubexd`、`metacubex/mihomo`、`calciumion/new-api`。

- **本环境已配置**：飞牛 daemon 已配 `registry-mirrors`（如 `docker.fnnas.com`），`ghcr.io` 与 `metacubex/mihomo` 均可直连，**可跳过本节**。
- **全新部署 —— 二选一**：
  1. **镜像加速地址**：飞牛 **Docker 设置 → 镜像加速** 填入可用加速地址（或编辑 `/etc/docker/daemon.json` 的 `registry-mirrors`），重启 Docker 后生效。
  2. **局域网代理**：把 Docker 的镜像源 / HTTP 代理指向局域网内的 Clash（如 `http://<LAN_PROXY_IP>:<PORT>`），让镜像拉取经局域网代理出网。
- 上述两种方式的代理**都只影响拉镜像，不影响运行时流量**。

> ⚠️ 镜像源 / 代理仅用于拉取，**不要当作运行时出网方式**。本栈的 ChatGPT 出网走的是 mihomo（见 §0），与镜像源无关。

---

## 3. 目录结构

把本项目复制到飞牛数据卷，例如 `/vol1/YOUR_USER_ID/local_aurora_api/`：

```
/vol1/YOUR_USER_ID/local_aurora_api/
├── .env                         # 本机 SESSION_SECRET，不提交
├── docker-compose.yml
├── config/
│   └── mihomo/
│       └── config.example.yaml # 首次启动示例
└── data/
    ├── mihomo/
    │   └── config.yaml         # 运行配置，会被订阅或 WebUI 更新
    └── new-api/                # New API 持久化数据
```

首次部署时初始化本地文件：

```bash
cd /vol1/YOUR_USER_ID/local_aurora_api
cp .env.example .env
mkdir -p data/mihomo data/new-api
cp config/mihomo/config.example.yaml data/mihomo/config.yaml
```

编辑 `.env`，把 `SESSION_SECRET` 设为 `openssl rand -hex 16` 生成的随机值。只在首次部署时复制 Mihomo 示例配置；已有 `data/mihomo/config.yaml` 时不要覆盖。

> 本栈不挂载 `access_tokens.txt`：token 在 New API 渠道密钥里填写，再通过 Aurora 的外部 token 能力使用（见 §8）。Aurora 也支持自己的 token 文件账号池，但那是另一种部署方式，本项目没有采用。本地备份只能放在被忽略的 `.secrets/`，不要复制到共享目录。
>
> **旧部署迁移边界**：2026-07-26 的 NAS 只读盘点发现，仍在运行的旧目录使用 `mihomo/`、`new-api-data/`，并向 Aurora 挂载 `access_tokens.txt`。本节描述的是当前仓库的新目标结构，不能直接覆盖旧目录。迁移前必须先备份并校验，保持原 `SESSION_SECRET`，再决定是否继续保留账号池挂载。

---

## 4. docker-compose.yml

项目根目录的 [`docker-compose.yml`](../docker-compose.yml) 是唯一 Compose 配置入口，不在本文重复维护副本。

其中：

- Mihomo 和 New API 的可变数据统一写入被 Git 忽略的 `data/`。
- Aurora 显式设置 `PROXY_URL`、`http_proxy` 和 `ENABLE_EXTERNAL_TOKEN`。
- New API 的 `SESSION_SECRET` 必须从本地 `.env` 提供；缺失时 Compose 会直接报错。

> 镜像拉取：镜像源配置见 §2，飞牛 daemon 已配 `registry-mirrors`（docker.fnnas.com），`ghcr.io` 与 `metacubex/mihomo` 均可直连。

---

## 5. 启动服务

在 `/vol1/YOUR_USER_ID/local_aurora_api/` 目录执行（镜像源已在 §2 确认可用）：

```bash
cd /vol1/YOUR_USER_ID/local_aurora_api
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
4. `external-controller` 开在 `0.0.0.0:9090`，供 metacubexd 面板连接（compose 已映射）。

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

new-api 官网 `latest` 镜像**首次启动是未初始化状态**，默认 `root/123456` 登录会失败。

1. 浏览器打开 `http://<NAS_IP>:3000`
2. 会自动跳到**初始化页面** → 设置管理员账号、密码、邮箱
3. 用刚设的账号登录

> 如果之前装过、数据库已初始化但忘了密码：备份并清库重建——
> ```bash
> cd /vol1/YOUR_USER_ID/local_aurora_api
> mv data/new-api/one-api.db data/new-api/one-api.db.bak-$(date +%Y%m%d-%H%M%S)
> docker compose up -d --force-recreate new-api
> ```
> 重启后日志出现 `system is not initialized and no root user exists` 即表示回到首次初始化状态。

---

## 8. 配置 aurora 渠道（核心，在此填入 access_token）

登录 new-api 后：

1. 左侧 **渠道管理** → **添加渠道**
2. 填写：
   - 类型：**OpenAI**
   - 名称：`aurora`（随意）
   - 基座地址：`http://aurora:8080`（compose 网络内用容器名）
   - 密钥：**填你的 ChatGPT access_token**（即 `eyJ...` 那串，获取方式见 §1）
   - 模型：填 `*` （或留空，后续从上游获取）
3. 保存

> 💡 **为什么 Compose 没有 token 文件**：本栈在 New API 渠道密钥中填写 ChatGPT token，并通过 `Authorization: Bearer <token>` 交给已启用 `ENABLE_EXTERNAL_TOKEN` 的 Aurora。Aurora 当前也支持挂载 `access_tokens.txt` 作为自己的账号池，但本项目没有采用该路径。不要把 `.secrets/access_tokens.txt` 挂载或提交。

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

以下结论来自 2026-07-26 使用 curl/Python 对 NAS 活端点的实际调用。它们描述的是当时运行中的 Aurora 构建，而不是根据上游文档推断。

#### 思考强度：切模型，不传 effort 参数

- `reasoning_effort` 和 Responses API 的 `reasoning.effort` 均返回 `422 Invalid conversation body`。
- 日常快速调用使用 `gpt-5-6-pro`。
- 复杂分析、代码和规划使用 `gpt-5-6-thinking`。
- `gpt-5-6-thinking` 的复杂问题表现更稳，但返回的 `reasoning_content` 为空；第三方客户端看不到思考过程。

因此，当前正式用法是切换模型，不在 New API 参数覆盖或请求体中强塞 effort 参数。

#### 图片生成：当前不可用

- Aurora 文档虽然提供 `/v1/images/generations` 和 `gpt-image-2`，但当前 `GET /v1/models` 不列出该图片模型，必须在渠道模型列表和令牌模型范围中手动添加。
- 活端点的最小图片请求仍稳定返回 `sentinel prepare failed`。
- 失败发生在 ChatGPT 图像生成的 sentinel 准备阶段，不是 New API 的 `model_not_found`。

当前部署不得把图片生成功能标记为可用。WorkBuddy 的 `supportsImages` 表示图片输入/看图，不代表该端点能够生成图片。

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

### 11.1 ChatGPT token 过期（每 ~30 天）

access token 有效期有限。到期现象：Aurora 返回 401 或 New API 渠道报错。
更新：
1. 浏览器登录 ChatGPT 后访问 <https://chatgpt.com/api/auth/session> ，从返回 JSON 的 `accessToken` 字段取出新 `eyJ...` token
2. new-api **渠道管理** → 编辑 aurora 渠道 → 密钥改为新 token → 保存（无需改 docker-compose）
（或上 `refresh_tokens.txt` 自动续期，需 aurora 支持）

### 11.2 mihomo 保持 GLOBAL 模式

在 Mihomo WebUI 中确认出站模式为 `GLOBAL`、GLOBAL 首选节点为新加坡解锁节点（见 §6.1）。切换到 `rule` 可能让 Aurora 改走其他节点。每次更新订阅后重新确认模式和节点选择。

### 11.3 不影响飞牛 IPv6 DDNS

当前设计不启用 Mihomo TUN，并且只有 Aurora 配置 `http_proxy`，因此不会主动接管飞牛系统路由。部署后仍应实际验证 IPv6 DDNS 和防火墙行为。

### 11.4 mihomo 公网暴露风险（安全提示，可选）

Mihomo 的 `7890` 代理端口和 `9090` 控制端口当前都映射到 NAS，首次启动配置也没有认证。若飞牛 IPv6 防火墙未限制这些端口，可能形成公网裸代理或开放控制接口。部署前必须在飞牛防火墙或路由器侧限制入站范围，或为 Mihomo 配置认证与控制密钥。

### 11.5 排障速查

| 现象 | 原因 | 解决 |
|---|---|---|
| 镜像拉取失败 / 超时 | Docker 镜像源不可用 | 先解决镜像源（§2），再 `docker compose up -d` |
| aurora 报 `no available account` | token 失效 / 渠道密钥填错 | 在 new-api 渠道重新填入有效 token（§8），`docker compose restart aurora` |
| new-api `root/123456` 登不上 | 镜像未初始化或密码已改 | 清库重建（§7） |
| 指定模型 `model_not_found` | rc.21 分发器未注册模型 | 渠道点「从上游获取」同步模型（§8） |
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
- [ ] 确认公网无法直接访问未认证的 `7890` 和 `9090`
