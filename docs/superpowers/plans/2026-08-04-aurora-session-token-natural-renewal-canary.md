# Aurora Session-token Natural Renewal Canary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变生产栈和现有 access-token canary 的前提下，用独立 session-only canary 验证 Aurora 2.5.0 在 access token 自然到期后能否依靠自身 10 分钟 health check 恢复。

**Architecture:** 新建并行的 Aurora-only Compose 候选，固定监听 `127.0.0.1:18082`，只读挂载专用 `session_tokens.txt`，复用现有 Mihomo 新加坡代理网络，不创建 New API。一个单请求、零重试的脱敏探针负责启动基线、到期失败和恢复验证；首次鉴权失败后最多观察 15 分钟，不增加宿主换新、自动重启或补偿调用。

**Tech Stack:** Docker Compose、Aurora 2.5.0 固定 digest、Python 3 标准库、`unittest`、Mihomo、FNOS、Git bundle。

## Global Constraints

- 不读取、打印、复制或提交 session/access token、Cookie、Credential、邮件正文或上游响应正文。
- 只允许元数据检查：文件存在性、类型、UID/GID、权限、只读挂载、JWT 到期时间和 SHA；不得输出 Token 或其哈希。
- 生产 `aurora`、`new-api`、`mihomo`、`metacubexd`、Compose、数据库、cron、n8n 和 SMTP 均保持不变。
- 当前 `aurora-capability-canary`、端口 `18080` 和 access-token 文件保持不变；新试验使用独立项目、容器和端口 `18082`。
- 候选镜像只允许 `ghcr.io/aurora-develop/aurora@sha256:358533a8cd6355222297c699338fe6cdc024c6f3d951fb2fb03422350b9b7627`，架构必须为 `linux/amd64`。
- session 文件必须位于 FNOS 新路径的 `.secrets/canary/`，归属 `65532:65532`、权限 `600`，容器目标固定为 `/home/nonroot/session_tokens.txt` 且只读。
- 真实探针固定为 `gpt-4o` 最小非流式聊天；每次调用只发送一个合成短提示，客户端不重试，只输出 HTTP 状态、固定分类和响应结构布尔值。
- Aurora 启动时允许自动使用 session token 向 ChatGPT 上游交换 access token；除此以外不直接调用 session 接口。
- 首次确认鉴权失败后，最多观察 15 分钟并跨过至少一个 10 分钟 health-check 周期；失败只通知用户，不修改凭据、不移动 Token、不重启或重建任何生产组件。
- 本地实现、commit/push、Git bundle、FNOS 只读预检、canary 创建、真实请求、持续观察和清理分别是独立门禁。

---

### Task 1: 增加独立 session-only Compose 合同

**Files:**
- Create: `docker-compose.session-renewal-canary.yml`
- Modify: `tests/test_canary_compose_contract.py`

**Interfaces:**
- Consumes: `.env.canary` 中现有 `AURORA_CANARY_AUTHORIZATION`；FNOS `.secrets/canary/session_tokens.txt`。
- Produces: Compose 项目 `aurora-session-renewal-canary`、服务/容器 `aurora-session-renewal-canary`、loopback URL `http://127.0.0.1:18082`。

- [ ] **Step 1: 写失败的 Compose 合同测试**

在 `tests/test_canary_compose_contract.py` 新增 `SESSION_COMPOSE`，并添加以下断言：

```python
SESSION_COMPOSE = ROOT / "docker-compose.session-renewal-canary.yml"

def test_session_renewal_canary_is_parallel_session_only_and_non_restarting(self):
    text = SESSION_COMPOSE.read_text(encoding="utf-8")
    self.assertIn("name: aurora-session-renewal-canary", text)
    self.assertIn('"127.0.0.1:18082:8080"', text)
    self.assertIn("source: ./.secrets/canary/session_tokens.txt", text)
    self.assertIn("target: /home/nonroot/session_tokens.txt", text)
    self.assertIn("read_only: true", text)
    self.assertIn('user: "65532:65532"', text)
    self.assertIn('restart: "no"', text)
    self.assertNotIn("access_tokens.txt", text)
    self.assertNotIn("new-api", text)
    self.assertNotIn("18080", text)
```

- [ ] **Step 2: 运行测试并确认因文件缺失而失败**

Run: `python -m unittest tests.test_canary_compose_contract -v`

Expected: FAIL，原因是 `docker-compose.session-renewal-canary.yml` 不存在。

- [ ] **Step 3: 创建最小 Compose 文件**

创建以下精确结构；不得加入 New API、数据卷、host network 或自动重启：

```yaml
name: aurora-session-renewal-canary

services:
  aurora-session-renewal-canary:
    image: ghcr.io/aurora-develop/aurora@sha256:358533a8cd6355222297c699338fe6cdc024c6f3d951fb2fb03422350b9b7627
    container_name: aurora-session-renewal-canary
    user: "65532:65532"
    profiles: ["session-renewal"]
    ports:
      - "127.0.0.1:18082:8080"
    environment:
      Authorization: ${AURORA_CANARY_AUTHORIZATION:?set the canary service key}
      ENABLE_EXTERNAL_TOKEN: "false"
      PROXY_URL: http://mihomo:7890
      http_proxy: http://mihomo:7890
    volumes:
      - type: bind
        source: ./.secrets/canary/session_tokens.txt
        target: /home/nonroot/session_tokens.txt
        read_only: true
    networks:
      - aurora-stack
    restart: "no"

networks:
  aurora-stack:
    name: aurora-stack_default
    external: true
```

- [ ] **Step 4: 运行 Compose 合同测试**

Run: `python -m unittest tests.test_canary_compose_contract -v`

Expected: PASS，现有 access-token canary 合同也保持通过。

### Task 2: 增加单请求、零重试的脱敏续期探针

**Files:**
- Create: `scripts/aurora_session_renewal_probe.py`
- Create: `tests/test_aurora_session_renewal_probe.py`

**Interfaces:**
- Consumes: 固定 URL `http://127.0.0.1:18082/v1/chat/completions`；默认 `.env.canary` 中唯一且符合严格无引号语法的 `AURORA_CANARY_AUTHORIZATION=value`。
- Produces: 单行 JSON，仅含 `classification`、`http_status`、`response_is_json`、`choices_present`、`message_present`、`content_nonempty`；退出码 `0` 仅代表 `pass`。

- [ ] **Step 1: 写失败测试**

测试必须覆盖：固定 loopback URL、无 `--allow-real-api` 时 fail closed、精确 Task 5 参数向量（含 `--json`）、只发一次请求、请求头精确为既有 canary 语义 `Authorization: Bearer <service-key>`、HTTP 200 合法结构为 `pass`、401 或 Aurora 固定的 `no available account` 错误为 `auth_failed`、403 为 `upstream_forbidden`、连接失败及成功/HTTP 错误正文读取阶段的 `TimeoutError`/`ConnectionResetError`/`OSError` 为 `unavailable`、无效 JSON/结构为 `invalid_response`，以及输出不含模拟响应正文、异常正文和 Authorization 值。env 测试必须锁定唯一严格语法，并证明 quoted、commented、`export`、前导空格、首尾/嵌入空白、重复、空值和 NUL 均在真实请求前 fail closed，且不能成为 `auth_failed`。

```python
def test_probe_makes_exactly_one_request_and_returns_sanitized_pass(self):
    calls = []
    result = MODULE.probe_once(
        "service-key",
        opener=fake_opener(calls, 200, {"choices": [{"message": {"content": "synthetic-ok"}}]}),
    )
    self.assertEqual(len(calls), 1)
    self.assertEqual(result["classification"], "pass")
    self.assertNotIn("synthetic-ok", json.dumps(result))
    self.assertNotIn("service-key", json.dumps(result))
```

- [ ] **Step 2: 运行测试并确认模块缺失**

Run: `python -m unittest tests.test_aurora_session_renewal_probe -v`

Expected: FAIL，原因是探针模块不存在。

- [ ] **Step 3: 实现最小探针**

实现以下固定接口，不加入重试、任意 URL、响应正文输出或 session-token 读取；`--json` 只是兼容标志，无论是否提供都输出同一固定单行 JSON：

```python
BASE_URL = "http://127.0.0.1:18082"

def probe_once(authorization: str, opener=urlopen) -> dict[str, object]:
    """Send exactly one synthetic gpt-4o request and return sanitized structure metadata."""

def read_env_value(path: Path, key: str) -> str:
    """Read exactly one non-empty KEY=value entry without returning any other env content."""

def render_json(result: dict[str, object]) -> str:
    """Serialize only the fixed allowlisted fields."""

def main(argv: list[str] | None = None) -> int:
    """Require --allow-real-api, read --env-file (default .env.canary), and return 0 only for pass."""
```

`.env.canary` 仅接受唯一一条精确 `AURORA_CANARY_AUTHORIZATION=value`：不得有引号、行内注释、`export`、行首空格、首尾/嵌入空白、空值、NUL 或 `$`。`$VAR`、`${VAR}`、`$$` 及任何 Compose 变量插值形式一律禁止；任何无效形式都必须在请求前返回非零和脱敏 `unavailable`，不得进入真实请求或被归类为自然续期的 `auth_failed`。请求头沿用既有 Aurora capability canary 的 service key 语义，固定为 `Authorization: Bearer <value>`。

请求体固定为 `model=gpt-4o`、`stream=false`、`max_tokens=8` 和非敏感合成提示；超时 60 秒。成功正文只在内存中解析结构并立即丢弃。错误正文最多读取 4 KiB，只允许把 HTTP 401 或固定的 `no available account of the requested type` 分类为 `auth_failed`；HTTP 403 单独分类为 `upstream_forbidden`，其他正文不进入输出或持久化文件。若成功响应或 `HTTPError` 的正文读取抛出 `TimeoutError`、`ConnectionResetError` 或其他 `OSError`，必须输出固定 `unavailable`，不输出异常正文，且进程返回非零。

- [ ] **Step 4: 运行探针测试**

Run: `python -m unittest tests.test_aurora_session_renewal_probe -v`

Expected: 全部 PASS。

### Task 3: 本地静态验证与发布门禁

**Files:**
- Modify: `docs/aurora_capability_canary.md`
- Modify after live result only: ignored `DEVLOG.md`

**Interfaces:**
- Consumes: Tasks 1-2 的 Compose 与探针。
- Produces: 可经 commit/push 和 Git bundle 传到 FNOS 的最小候选；本任务本身不发布。

- [ ] **Step 1: 用非敏感占位值静态解析 Compose**

Run in WSL:

```bash
AURORA_CANARY_AUTHORIZATION='non-sensitive-session-renewal-key' \
docker compose -f docker-compose.session-renewal-canary.yml --profile session-renewal config --format json >/dev/null
```

Expected: exit `0`；不得执行 `pull`、`create`、`up`、`start` 或 `run`。

- [ ] **Step 2: 运行相关测试与仓库检查**

Run:

```powershell
python -m unittest tests.test_canary_compose_contract tests.test_aurora_session_renewal_probe -v
git diff --check
```

Expected: tests 全部 PASS；`git diff --check` exit `0`。

- [ ] **Step 3: 更新运行手册的精确命令和判定表**

只补充 `18082`、独立 Compose、严格 env 语法、`Bearer` service key 语义、含 `--json` 的规范命令、探针三次调用上限和 `pass/auth_failed/unavailable/invalid_response` 判定，不把未执行现场步骤写成已完成。

- [ ] **Step 4: 停在 Git 发布门禁**

列出精确变更文件和测试结果。不得自动暂存、提交或 push；用户须另行批准 commit/push 和仅包含批准提交范围的 Git bundle/FNOS fast-forward。

### Task 4: FNOS 只读前置核对

**Files:**
- Read-only checkout: `/vol1/1000/Solis_Aurora_Gateway`

**Interfaces:**
- Consumes: 已通过 Git bundle 对齐的批准提交。
- Produces: `preflight=PASS|FAIL` 及固定布尔/枚举元数据；不创建容器或文件。

- [ ] **Step 1: 核对 Git 和现有运行基线**

核对新路径 checkout 为 clean `main` 且 HEAD 等于批准提交；生产四容器和当前 access-token canary 保持 running、重启次数不变；不得读取日志正文。

- [ ] **Step 2: 核对隔离资源**

确认 `127.0.0.1:18082` 未占用、`aurora-session-renewal-canary` 不存在、`aurora-stack_default` 存在、候选 image RepoDigest/架构/用户与批准值匹配、Mihomo 当前仍是新加坡策略。

- [ ] **Step 3: 核对 session 文件元数据**

只返回以下字段：`exists`、`regular_file`、`not_symlink`、`uid_is_65532`、`gid_is_65532`、`mode_is_600`、`nonempty_line_count_is_1`。不得返回文件名、大小、内容、哈希或行长度。

任一项失败即停止；不得创建、复制、改权或修复文件。

### Task 5: 创建并验证并行 session canary

**Files:**
- Runtime-only: FNOS ignored `.env.canary` and `.secrets/canary/session_tokens.txt`

**Interfaces:**
- Consumes: Task 4 PASS 与单独的创建/真实请求授权。
- Produces: 运行中的 `aurora-session-renewal-canary` 和一次脱敏 bootstrap 结果。

- [ ] **Step 1: 核对严格 env 语法并静态解析 FNOS Compose**

先以不输出 value 的方式确认既有权限 `600` 的 `.env.canary` 中 `AURORA_CANARY_AUTHORIZATION=value` 唯一且满足 Task 2 严格语法；任何 quoted、commented、`export`、前导空格、首尾/嵌入空白、重复、空值、NUL、`$VAR`、`${VAR}`、`$$` 或任何 Compose 变量插值均停止。通过后才执行 `docker compose ... config -q`，不得输出解析结果。

- [ ] **Step 2: 只创建并启动 session canary**

Run from `/vol1/1000/Solis_Aurora_Gateway`:

```bash
docker compose --env-file .env.canary \
  -f docker-compose.session-renewal-canary.yml \
  --profile session-renewal up -d aurora-session-renewal-canary
```

不得执行 `down`、不得重建现有 canary 或生产服务。启动过程允许 Aurora 自行用 session token 交换 access token。

- [ ] **Step 3: 验证容器边界**

只核对 running、restart count `0`、Compose 项目/服务标签、loopback `18082`、单一 session 文件只读挂载、UID/GID 和 Mihomo 网络；不得读取容器日志或凭据正文。

- [ ] **Step 4: 执行一次 bootstrap 探针**

Run:

```bash
python3 scripts/aurora_session_renewal_probe.py --allow-real-api --env-file .env.canary --json
```

环境中只注入既有 Aurora canary 服务 key。预期 `classification=pass`、HTTP `200`、结构布尔值全 true；不输出正文。失败即停止并通知，不自动重建或更换凭据。

### Task 6: 自然到期与一次内部自愈周期验收

**Files:**
- Runtime state: no new persistent file
- Modify after verdict only: ignored `DEVLOG.md`

**Interfaces:**
- Consumes: Task 5 bootstrap PASS、另行批准的持续观察/真实请求门禁。
- Produces: `renewal_result=PASS|FAIL|NOT_OBSERVED`，不产生 Token 或响应正文记录。

- [ ] **Step 1: 确定首次到期观察时间**

只对现有 canary access-token 快照做 JWT `exp` 元数据检查，不打印 Token、哈希、文件名或其他 JWT claims。该时间只是 session canary 内部 token 到期时间的近似观察点，不构成同一性证明；如果届时仍为 PASS，则结果保持 `NOT_OBSERVED`，以后每 60 分钟最多调用一次，直到首次 `auth_failed` 或用户停止观察。

跨小时等待使用单独批准的临时 Codex 自动化，不新增 FNOS cron。自动化在未到观察时间时不连接 FNOS；到时只执行一次脱敏探针，并在最终 PASS、FAIL 或用户停止后删除。

- [ ] **Step 2: 记录首次自然鉴权失败**

在到期观察点执行一次零重试探针。只有 `classification=auth_failed` 才启动 15 分钟自愈窗口；网络不可达或响应结构异常分别记为 `unavailable`/`invalid_response`，不得冒充 Token 到期。

- [ ] **Step 3: 跨过 Aurora health-check 周期**

从首次 `auth_failed` 起不修改任何状态，不调用 session 接口，不重启容器。等待 12 分钟后执行一次探针；如果 PASS，则自然续期验收 PASS。

- [ ] **Step 4: 执行最终门禁探针**

若 12 分钟探针仍为 `auth_failed`，在首次失败满 15 分钟时执行最后一次探针。PASS 则验收 PASS；否则验收 FAIL 并停止所有自动推进。观察窗内真实模型请求总数最多三次：首次失败、12 分钟复核、15 分钟最终复核。

- [ ] **Step 5: 报告并通知**

PASS 只报告时间、三次以内的 HTTP 状态/分类、容器仍 running、restart `0`、挂载仍只读、生产基线不变。FAIL 仅发布脱敏 FAIL 并通知用户人工修补；不得修改凭据、移动 Token、重启/重建容器、恢复宿主换新或调用其他补偿接口。

- [ ] **Step 6: 停在后续门禁**

自然续期 PASS 不授权 canary 重启恢复、生产迁移、cron/n8n 修改或清理。重启恢复必须另立门禁；session canary 与证据是否保留或删除也由用户单独决定。

## Completion Criteria

- 并行 session canary 不影响生产和当前 access-token canary。
- bootstrap 请求 PASS，且自然到期后真实出现一次 `auth_failed`。
- Aurora 在首次失败后 15 分钟内、不经宿主换新/重启/补偿，自行恢复为结构合法的 HTTP `200`。
- canary restart count 保持 `0`；session 文件始终 `600`、`65532:65532`、容器内只读。
- 生产容器、Compose、数据库、cron、n8n、SMTP、Git 和代理策略无变化。
- 若未观察到真实 `auth_failed`，结果只能是 `NOT_OBSERVED`，不得宣称自然续期通过。
