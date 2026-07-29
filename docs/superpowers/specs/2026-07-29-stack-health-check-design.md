# local_aurora_api 一键健康检查设计

## 目标

为 fnOS 上的 `local_aurora_api` 提供一个只读、可测试的一键健康检查入口：

```bash
python3 scripts/check_stack_health.py
```

脚本直接在 NAS 项目目录运行，检查 Docker、SQLite、Token、定时续期日志、Mihomo 出口、New API 模型和真实聊天链路。默认输出中文摘要，并可通过 `--json` 输出稳定的机器可读结果。

## 非目标

- 不修改数据库、Token、容器、代理选择、Compose 或 cron。
- 不执行图片生成、文件上传、TTS、工具调用或 Responses API 测试。
- 不替代现有 `refresh_chatgpt_access_token.py`，也不改变其行为。
- 不在本轮增加定时任务、监控服务、第三方依赖或 Windows 包装脚本。
- 不把客户端令牌、渠道 Token、session token、聊天正文或响应正文写入输出。

## 命令行接口

```text
python3 scripts/check_stack_health.py [--root PATH] [--channel-id ID] [--json]
```

- `--root`：项目根目录，默认取脚本父目录的父目录；保留该参数便于单元测试和非默认安装路径。
- `--channel-id`：New API 的 Aurora 渠道 ID，默认 `1`，与续期脚本保持一致。
- `--json`：输出单个 JSON 对象；未指定时输出中文摘要。

脚本始终执行完整检查，包括一次真实聊天请求；不提供跳过聊天的默认分支。

## 检查模型

每项检查返回统一结果：

```text
name      稳定的检查标识
status    PASS / WARN / FAIL
summary   不含敏感信息的中文摘要
details   仅包含该检查允许公开的结构化字段
```

所有检查完成后再计算总体状态：

- 任一 `FAIL` → 总体 `FAIL`，进程退出码 `1`。
- 没有 `FAIL`、至少一个 `WARN` → 总体 `WARN`，进程退出码 `0`。
- 全部 `PASS` → 总体 `PASS`，进程退出码 `0`。

单项失败不得阻止后续检查，除非后续检查缺少不可替代的前置数据。例如读不到客户端令牌时，模型与聊天检查分别报告依赖失败，而其他本地检查继续执行。

## 检查项

### 1. Docker 容器

固定检查：

- `aurora`
- `new-api`
- `mihomo`
- `metacubexd`

通过 `docker inspect` 读取状态和 `RestartCount`，不读取或输出容器环境变量。

- 四个容器均为 `running` 且重启次数为 `0` → `PASS`
- 容器缺失、非 `running` 或重启次数大于 `0` → `FAIL`

允许输出容器名、运行状态和重启次数，不输出完整 inspect 数据。

### 2. New API SQLite 与渠道 Token

以 SQLite 只读 URI 打开 `data/new-api/one-api.db`：

1. 执行 `PRAGMA integrity_check`。
2. 读取指定渠道的 key，仅在内存中解析 JWT `exp`。
3. 确认至少存在一枚仍启用且可用的 New API 客户端令牌，供后续模型与聊天检查使用。

状态规则：

- `integrity_check = ok` → 数据库部分通过；否则 `FAIL`
- Token 格式无效或已过期 → `FAIL`
- Token 剩余时间大于 72 小时 → `PASS`
- Token 剩余时间在 0 到 72 小时之间 → `WARN`
- 没有可用客户端令牌 → `FAIL`

只输出完整性结果、剩余秒数/小时数和到期时间；不得输出任何 key 或其哈希。

### 3. 定时续期日志

读取 `.secrets/token-refresh.log`，逐行解析 JSON，只保留下列白名单字段：

- `time`
- `event`
- `remaining_seconds`
- `channel_id`
- `reason`
- `previous_exp`
- `new_exp`
- `extension_seconds`

状态规则：

- 最新合法事件为 `refresh_skipped` 或 `refresh_succeeded` → `PASS`
- 日志尚不存在或为空 → `WARN`
- 存在非法 JSON，但仍能找到合法事件 → `WARN`
- 文件非空却没有合法事件，或最新事件为 `refresh_failed` → `FAIL`

`reason` 只允许来自现有续期脚本的安全错误消息，并在输出前执行长度限制和敏感值替换。

### 4. Mihomo 模式与新加坡出口

1. 从容器网络地址访问 Mihomo 控制 API，确认运行模式为 `GLOBAL`。
2. 读取 `GLOBAL` 当前选择，只输出节点名称，不修改选择。
3. 经 Mihomo HTTP 代理访问 Cloudflare trace，解析 `loc`，要求为 `SG`。

状态规则：

- `GLOBAL` 且 `loc=SG` → `PASS`
- 控制 API、代理请求失败，模式不是 `GLOBAL`，或出口不是 `SG` → `FAIL`

不输出订阅地址、完整 Mihomo 配置或代理凭据。

### 5. New API 模型范围

使用内存中的客户端令牌请求：

```text
GET http://127.0.0.1:3000/v1/models
```

模型 ID 必须严格等于：

- `gpt-5-6-pro`
- `gpt-5-6-thinking`

缺少模型或出现额外模型均为 `FAIL`。输出仅包含排序后的模型 ID。

### 6. 真实聊天链路

向 New API 发送一个带唯一随机标记的最小非流式聊天请求，避免固定提示触发上游空响应缓存：

1. 先尝试 `gpt-5-6-pro`。
2. 如果请求或结构无效，再尝试 `gpt-5-6-thinking`。
3. 验证 HTTP 成功、`choices`、`message` 和 `content` 字段结构。

状态规则：

- pro 返回结构合法且正文非空 → `PASS`
- pro 返回结构合法但正文为空 → `WARN`
- pro 失败、thinking 返回结构合法 → `WARN`
- 两个模型都发生网络/HTTP/鉴权错误或结构无效 → `FAIL`

聊天正文、随机标记、请求体和 Token 不进入输出。合法 HTTP 200 空 completion 不得被误判为鉴权失败。

## 输出格式

### 中文摘要

每项一行：

```text
[通过] 容器：4/4 运行，重启次数均为 0
[警告] Token：剩余 48 小时，已进入续期窗口
[失败] 代理：当前出口不是 SG
```

最后输出：

```text
总体：通过
```

总体中文值为 `通过`、`警告` 或 `失败`。

### JSON

```json
{
  "checked_at": "2026-07-29T12:00:00+08:00",
  "overall": "PASS",
  "checks": [
    {
      "name": "containers",
      "status": "PASS",
      "summary": "4/4 运行，重启次数均为 0",
      "details": {}
    }
  ]
}
```

JSON 使用 UTF-8，标准输出只包含这一个对象，便于后续监控读取。

## 错误处理与脱敏

- Docker、SQLite、HTTP 和文件错误转换为短错误代码与安全中文摘要。
- 默认不输出 traceback、原始 HTTP body、完整 Docker inspect 或 SQLite 行。
- 已读取的 Token 仅作为内存变量使用；所有异常文本在输出前替换已知敏感值。
- 字符串详情设置长度上限，避免上游 HTML、sentinel 页面或响应正文进入日志。
- 脚本不得创建、修改或删除任何文件。

## 代码边界

新增：

- `scripts/check_stack_health.py`
- `tests/test_check_stack_health.py`

更新：

- `README.md`：增加用户可见的一键检查命令和结果含义。
- `docs/fnos_deployment.md`：增加完整运行、JSON 输出和退出码说明。

健康检查可以复用 `refresh_chatgpt_access_token.py` 中无副作用的 JWT、客户端令牌和 HTTP 辅助逻辑，但不得调用更新数据库、重启容器或换取 Token 的函数，也不得改变续期脚本的既有行为。

## 测试策略

本地测试使用 Python 标准库 `unittest` 和 `mock`，不要求 Docker、NAS、网络或真实凭据。

最低覆盖：

1. 全部检查通过。
2. 容器缺失、停止或重启次数非零。
3. SQLite 完整性失败。
4. Token 临近到期产生 `WARN`，过期产生 `FAIL`。
5. 续期日志缺失、非法 JSON、`refresh_failed` 和正常事件。
6. Mihomo 非 `GLOBAL`、控制 API失败、出口非 `SG`。
7. 模型缺少或多出模型。
8. pro 非空聊天通过。
9. 合法空 completion 产生 `WARN`。
10. pro 失败但 thinking 成功产生 `WARN`。
11. 两个聊天模型都失败产生 `FAIL`。
12. `--json` 可解析，且输出不包含测试 Token。
13. 任一 `FAIL` 时退出码为 `1`，只有 `PASS/WARN` 时为 `0`。

## 验收标准

实现完成需满足：

- 新增单元测试全部通过，现有续期测试保持通过。
- `python -m py_compile` 通过。
- `git diff --check` 通过。
- Markdown 相对链接有效。
- `.env`、`.secrets/`、`data/`、本地维护文档继续被 Git 忽略。
- 凭据特征扫描无新增命中。
- 本地只验证纯函数和模拟边界；部署 NAS 与真实运行必须另行获得用户授权，未部署前不得声称 NAS 健康检查已上线。
