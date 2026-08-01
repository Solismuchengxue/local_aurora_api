# Solis_Aurora_Gateway n8n 离线健康状态接入设计

## 状态

- 设计日期：2026-08-01。
- 评审收敛日期：2026-08-01。
- 本文只定义候选实现，不代表已经部署、导入、验证或激活。
- 当前阶段不得修改 `Solis_Studio_OS`；待该项目的验证与汉化工作完成，并获得用户单独批准后，才能实现其工作流部分。
- 设计文档、实施计划、代码、FNOS 部署、n8n 导入、邮件验证、激活、提交和推送分别受独立门禁控制。

## 背景与已确认事实

`Solis_Aurora_Gateway` 当前已有 `scripts/check_stack_health.py`，用于人工执行完整健康检查。该脚本会检查代理出口、模型列表并发起真实聊天，因此不能直接作为无人值守的 n8n 状态生产器。

FNOS 只读预检已确认：

- 现有 n8n 与 PostgreSQL 容器健康；
- `Solis_Studio_OS/data` 当前以读写 bind mount 挂载为 n8n 容器内的 `/exchange`；“工作流只读”目前是节点行为约束，不是文件系统级强制边界；
- n8n 文件访问白名单精确为 `/exchange`；
- 现有 SMTP Credential 可以在实例内复用，但不得读取、导出或写入仓库；
- 当前没有 Aurora 工作流、Aurora 状态目录或 Aurora 到 `/exchange` 的状态生产链路；
- Studio OS 的本地恢复包会归档整个 `data` 目录，因此未来的 Aurora 状态文件会进入本地恢复包和加密云备份。

## 目标

建立一条边界清晰、默认静默的本地监控链路：

```text
04:17 / 16:17 既有续期检查
          ↓
05:12 / 17:12 Aurora 离线状态生产器
          ↓ 原子替换
/vol1/1000/Solis_Studio_OS/data/ops/aurora-gateway/latest.json
          ↓ n8n 容器内 /exchange/ops/aurora-gateway/latest.json
05:15 / 17:15 n8n 严格校验
          ↓
PASS 静默；WARN、FAIL、缺失、过期或格式错误才进入邮件节点
```

首版只与现有两次续期周期对齐，不实现高频容器监控、状态变化去重或告警冷却。这样最多每天产生两次异常通知，避免为首版引入持久化告警状态。所有时刻均按 `Asia/Shanghai` 解释；生产器写入 UTC `generated_at`，n8n 在同一时区校验。

现有续期脚本的静态最坏路径约为 48 分钟：Token 交换最多 90 秒，上游模型与四次聊天结构验证最多约 774 秒，容器重启最多 60 秒，New API 就绪等待与验证最多约 894 秒；失败回退可能再次执行数据库替换、重启和约 894 秒验证。原先的 04:23/04:25 与 16:23/16:25 方案不能覆盖该路径，因此改为续期开始后 55 分钟生产、58 分钟检查，并要求生产器探测既有续期锁。05:12/17:12 仍检测到续期锁时，发布 `FAIL / refresh_in_progress_overrun`；不把超时运行误报为正常，也不增加第二条延迟复查流程。

## 非目标

- 不请求模型列表，不调用真实聊天、Responses API 或其他模型接口。
- 不检查公网代理出口，不访问 Cloudflare 或其他外部网络资源。
- 不调用 FNOS 私有 API。
- 离线生产器运行时不修改 Token、SQLite、续期日志、容器、Compose、代理选择或 cron；后续受控部署中的 Compose 与 cron 变更另受独立门禁约束。
- 不读取或输出凭据正文、客户端令牌、渠道 Token、连接串、SMTP 信息、邮箱、Cookie、订阅地址、敏感路径、敏感配置正文、业务正文、原始日志或原始命令输出。
- 不把 Docker Socket 挂载给 n8n，也不扩大 `/exchange` 文件访问白名单。
- 文件型状态生产与读取不新增或复用任何 Credential；SMTP 只属于状态校验完成后的通知动作。
- 不复用或改造现有完整健康检查的命令行入口。
- 不在首版实现 Prometheus、Webhook、告警确认、自动修复、容器重启或历史指标数据库。

## 方案比较

### 方案 A：独立离线生产器与受管状态文件

Aurora 项目拥有一个新的最小生产器；Aurora 是自身运行状态的唯一权威，Studio OS 与 n8n 只消费脱敏状态，不得反向修改 Aurora 的容器、数据、配置、Token、cron 或状态源。

优点：

- 无人值守入口与真实 API 检查物理分离，审计边界最清楚；
- n8n 不需要 Docker 权限、Aurora Token 或跨网络访问；
- 状态契约可以独立测试和版本化；
- 生产器故障自然表现为文件缺失或过期。

代价：

- 需要一个新脚本、一个受管 cron 条目和一个独立工作流；
- 跨项目契约需要两边文档保持一致。

### 方案 B：扩展现有完整健康检查

为 `check_stack_health.py` 增加离线模式与状态文件参数。

优点是文件较少；缺点是完整脚本仍包含真实模型和聊天调用，参数误用或未来重构容易模糊安全边界。无人值守任务不应依赖“必须传对一个跳过真实调用的参数”。

### 方案 C：n8n 直接读取 Docker 或调用 Aurora API

该方案需要挂载 Docker Socket、扩大 n8n 权限或向 n8n 提供 API 凭据。它违背 Studio OS 只通过 `/exchange` 消费受管状态文件的现有边界，因此排除。

### 决策

采用方案 A。

## 项目所有权

### Solis_Aurora_Gateway

未来候选文件：

- 新建 `scripts/write_n8n_health_status.py`：只读采集、脱敏聚合和原子写入；
- 新建 `tests/test_write_n8n_health_status.py`：标准库单元测试；
- 更新 `DESIGN.md`：记录正式边界和跨项目状态契约；
- 更新 `docs/fnos_deployment.md`：记录候选 cron、部署门禁和回退步骤。

首版沿用仓库当前在部署指南中管理 cron 的做法，不新增 cron 框架或第三方依赖。

### Solis_Studio_OS

待其当前验证与汉化工作完成后，未来候选文件：

- 新建 `workflows/aurora-gateway-alert.workflow.json`；
- 新建 `tests/workflows/test_aurora_gateway_alert_export.py`；
- 更新 `workflows/README.md`；
- 按正式架构职责检查 `DESIGN.md` 与 `docs/architecture.md` 是否需要同步。

本设计阶段不修改上述文件。

## `/exchange` 强制只读边界

现有 `/vol1/1000/Solis_Studio_OS/data:/exchange` 挂载必须保持读写，因为 Studio OS 的其他受管工作流需要写入交换目录。仅依赖 n8n 节点选择“读取”操作不能构成安全边界；宿主机生产者与 n8n 进程当前还使用相同 UID，普通属主权限也不能可靠区分两者。

首版候选 Compose 约束是在现有读写父挂载之上，为 Aurora 状态子目录增加覆盖式只读 bind mount：

```yaml
volumes:
  - /vol1/1000/Solis_Studio_OS/data:/exchange
  - type: bind
    source: /vol1/1000/Solis_Studio_OS/data/ops/aurora-gateway
    target: /exchange/ops/aurora-gateway
    read_only: true
    bind:
      create_host_path: false
```

Docker 官方文档确认 bind mount 默认可写，`read_only`/`ro` 用于阻止容器写入，并且挂载到已有容器目录时会遮蔽该位置的原内容。由于“读写父挂载 + 只读子挂载”的重叠行为仍需在 FNOS 当前 Docker/Compose 版本上验证，本设计把以下门禁设为强制条件：

参考：[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)、[Compose services volumes](https://docs.docker.com/reference/compose-file/services/#volumes)。

1. 部署前只读门禁确认当前 Compose 版本、现有父挂载、目标路径和回退基线，不创建目录或重建容器。
2. 获得部署批准后，使用 `docker compose config -q` 验证候选配置，并只重建 n8n。
3. `docker inspect` 必须同时显示父挂载为读写、Aurora 子挂载为只读。
4. 在部署验证阶段，使用专用无残留哨兵名尝试从 n8n 容器写入子目录；操作必须因只读文件系统失败，宿主机不得出现该文件。
5. 同时验证既有 `/exchange` 其他受管写入路径仍保持读写。

任一条件不满足，立即恢复变更前 Compose 与 n8n 容器，停止导入工作流。不得以节点行为、UID、约定或“没有写节点”为理由绕过该阻塞项。

## 生产器命令行接口

候选命令：

```text
python3 scripts/write_n8n_health_status.py \
  --root /vol1/1000/Solis_Aurora_Gateway \
  --output /vol1/1000/Solis_Studio_OS/data/ops/aurora-gateway/latest.json \
  --channel-id 1
```

- `--root` 默认取脚本父目录的父目录，部署时仍显式传入正式路径。
- `--output` 必须显式提供；生产器不得猜测 Studio OS 路径。
- `--channel-id` 默认值为 `1`，与现有续期脚本保持一致。
- 生产器不创建输出父目录；目录缺失或权限不正确时失败关闭。

退出码：

- `0`：已原子发布合法的 `PASS` 或 `WARN` 状态；
- `1`：已原子发布合法的 `FAIL` 状态；
- `2`：生产器自身错误，未能发布新状态。n8n 随后通过缺失或过期文件发现该故障。

## 离线检查模型

每项检查只返回：

```text
status  PASS / WARN / FAIL
code    固定白名单错误码
details 仅允许布尔值、整数和固定枚举
```

总体状态优先级为 `FAIL > WARN > PASS`。

### 1. 容器状态

固定检查 `aurora`、`new-api`、`mihomo` 和 `metacubexd`。

- 四个容器都存在且为 `running`：`PASS / containers_running`；
- 任一容器缺失或不是 `running`：`FAIL / container_state_invalid`。

允许输出预期数、运行数和每个固定容器的重启计数。重启计数首版只作为诊断元数据，不参与总体状态；否则一次历史重启会永久触发重复告警。生产器不得输出完整 `docker inspect`、容器环境变量或 Compose 配置文件列表。

### 2. 运行契约

使用限定格式的 `docker inspect` 验证：

- Compose 项目标识严格为 `aurora-stack`；
- Compose 工作目录严格为 `/vol1/1000/Solis_Aurora_Gateway`；
- `new-api` 的 `/data` 绑定到正式项目的 `data/new-api`；
- `mihomo` 的 `/root/.config/mihomo` 绑定到正式项目的 `data/mihomo`。

全部匹配时为 `PASS / runtime_matches`；任一不匹配为 `FAIL / runtime_mismatch`。状态文件只保存每项是否匹配，不保存原始标签、原始挂载列表或敏感配置路径。

### 3. 本地 TCP

只执行 TCP 建连，不发送 HTTP 请求或应用层正文：

- Aurora `8080`；
- New API `3000`；
- MetaCubeXD `9097`；
- Mihomo `9090`：先要求限定的 Docker 端口字段存在且格式合法，再从限定的容器网络字段取得桥接 IPv4 进行 TCP 建连；两个地址都只在内存中使用。

四项均可达时为 `PASS / local_ports_reachable`；任一不可达为 `FAIL / local_port_unreachable`。每项超时 2 秒，状态文件只保存固定服务名与布尔结果，不保存探测到的宿主机地址。

2026-08-01 的 FNOS 只读预检显示，Mihomo 容器桥接地址可达，但 FNOS 主机回环到其指定 LAN 发布地址超时；这不是 Mihomo 服务故障。为避免把 FNOS 的主机回环限制误报为服务不可达，Mihomo 使用桥接地址探测，同时保留发布绑定元数据门禁。其余三个通配发布端口仍规范化为本机回环地址探测。

### 4. SQLite 与渠道 Token 元数据

以 SQLite 只读 URI 打开正式数据库：

1. 执行 `PRAGMA integrity_check`；
2. 读取指定启用渠道的渠道 Token，仅在内存中解析 JWT 到期时间；
3. 不读取 New API 客户端令牌，因为离线生产器不需要调用模型 API。

规则：

- 数据库不完整、渠道不存在、Token 格式无效或已过期：`FAIL`；
- Token 剩余时间大于 0 且不超过 72 小时：`WARN / token_near_expiry`；
- Token 剩余时间大于 72 小时：`PASS / database_and_token_valid`。

只输出完整性布尔值、剩余秒数和到期时间，不输出 Token、Token 哈希或数据库行。

### 5. 续期事件元数据

只解析既有续期日志中的白名单字段，不把原始行、异常正文或任意字符串写入状态文件。

- 生产器先以非阻塞方式探测既有续期锁；05:12/17:12 仍被占用时发布 `FAIL / refresh_in_progress_overrun`，不继续读取可能变化中的状态；
- 最新合法事件为成功或跳过，且事件不早于 13 小时：`PASS / refresh_recent`；
- 日志缺失、为空或没有合法事件：`WARN / refresh_missing`；
- 最新合法事件早于 13 小时：`WARN / refresh_stale`；
- 存在部分非法行但仍有近期合法成功事件：`WARN / refresh_malformed`；
- 最新合法事件为失败：`FAIL / refresh_failed`。

状态详情只保存固定事件枚举、合法/非法记录计数和事件时间。

## 状态文件契约

文件编码为 UTF-8，无 BOM，以换行结尾，最大 16 KiB。顶层和检查集合必须严格匹配 Schema v1：

```json
{
  "schema_version": 1,
  "producer": "Solis_Aurora_Gateway",
  "generated_at": "2026-08-01T09:12:00Z",
  "overall": "PASS",
  "checks": {
    "containers": {
      "status": "PASS",
      "code": "containers_running",
      "details": {
        "expected": 4,
        "running": 4
      }
    },
    "runtime_contract": {
      "status": "PASS",
      "code": "runtime_matches",
      "details": {
        "project_matches": true,
        "working_dir_matches": true,
        "mounts_match": true
      }
    },
    "local_tcp": {
      "status": "PASS",
      "code": "local_ports_reachable",
      "details": {
        "expected": 4,
        "reachable": 4
      }
    },
    "database": {
      "status": "PASS",
      "code": "database_and_token_valid",
      "details": {
        "integrity_ok": true,
        "remaining_seconds": 604800,
        "expires_at": "2026-08-08T09:12:00Z"
      }
    },
    "refresh_log": {
      "status": "PASS",
      "code": "refresh_recent",
      "details": {
        "event": "refresh_skipped",
        "valid_records": 8,
        "invalid_records": 0,
        "event_at": "2026-08-01T08:17:02Z"
      }
    }
  }
}
```

不得增加自由文本摘要、原始错误、原始日志、原始命令输出、日志路径、数据库路径、敏感路径、容器标签、挂载源、主机地址、Token、连接串、邮箱、Cookie、Credential 或业务正文字段。

## 状态保留与备份传播

- 活动目录只允许一个滚动文件 `latest.json`；生产器不得创建历史状态、逐次日志、日期文件或无界归档。
- 原子写入临时文件只在一次调用期间存在；成功或失败后不得遗留临时文件。
- `latest.json` 会随 Studio OS 的整个 `data` 目录进入本地恢复包和加密云备份。每个恢复包最多增加一份不超过 16 KiB 的脱敏快照。
- 备份副本继承 Studio OS 现有恢复包和加密云备份的保留、候选清理与人工批准规则；本集成不获得删除恢复包或云端副本的权限。
- 由于备份副本可能长期保留，状态文件必须按“可长期进入加密归档”的标准设计，禁止任何凭据、连接串、Cookie、邮箱、敏感路径、原始日志、原始命令输出或业务正文。
- 回退时默认保留当前 `latest.json` 作为状态证据；删除活动文件、状态目录或备份副本均需独立批准。

## 原子写入

1. 输出父目录必须预先存在，并解析为普通目录而非符号链接。
2. 临时文件只在同一父目录创建，名称由脚本生成。
3. JSON 序列化后先验证大小不超过 16 KiB。
4. 临时文件在 POSIX 上设置为 `0600`，写入后执行 `flush` 与 `fsync`。
5. 使用 `os.replace()` 原子替换 `latest.json`。
6. 尝试刷新父目录元数据；平台不支持时只允许忽略明确的“不支持”错误。
7. 写入失败时仅清理由本次调用创建的临时文件，不删除或截断既有 `latest.json`。

单项健康检查失败仍必须发布合法的 `WARN` 或 `FAIL` 文档；只有生产器自身无法形成或原子发布合法文档时，才保留旧文件并退出 `2`。

## n8n 工作流候选

工作流名称候选为 `Solis Aurora Gateway Alert (Phase 1)`，仓库导出必须保持 `active=false`、时区为 `Asia/Shanghai`，且不包含实例 ID、邮箱、Credential 引用或密钥。

节点职责：

1. `每天 05:15、17:15 检查`：两次日程入口；
2. `手工合成告警`：生成明确标记为测试的固定失败文档，不写真实状态文件；
3. `读取 Aurora 状态`：只读 `/exchange/ops/aurora-gateway/latest.json`；
4. `提取状态文本`：把文件转换为文本；
5. `严格校验状态`：解析 JSON、验证 Schema 并生成脱敏告警模型；
6. `仅异常时继续`：`PASS` 静默，其他结果进入邮件；
7. `发送告警邮件`：由实例内人工绑定现有 SMTP Credential。该 Credential 只用于下游通知，不参与状态文件读取、解析或鉴权。

校验规则：

- 文件文本大于 16 KiB、JSON 无效、顶层字段或检查集合不匹配均视为格式错误；
- `schema_version` 必须为 `1`，`producer` 必须精确匹配；
- `generated_at` 必须是合法 UTC 时间；距当前超过 15 分钟为过期，超前超过 5 分钟为时钟异常；
- 每个 `status` 和 `code` 必须来自白名单；
- n8n 根据五项状态重新计算总体状态；与文件中的 `overall` 不一致时告警；
- 邮件只包含检查名、固定状态码、生成时间和固定中文解释，不回显原始文件或任意未知字段。

告警条件：

- `PASS`：静默；
- `WARN`：发送告警，表示需要人工关注但服务尚未确认中断；
- `FAIL`：发送告警；
- 文件缺失、不可读、过期、超前、过大、格式错误或总体状态矛盾：发送告警；
- 手工合成入口：主题必须带 `[TEST]`，正文明确说明不代表真实故障。

首版不做告警去重。两次日程限制了最坏情况下的邮件频率；若未来改为高频监控，必须先另行设计状态变化去重与冷却窗口。

### Credential 与未来数据库边界

- 文件型首版的状态生产、挂载、读取和校验不新增或复用任何 Credential。
- 现有 SMTP Credential 只用于异常状态形成后的邮件通知；不得把它解释为状态源 Credential，也不得导出其引用对象或正文。
- 首版不把状态写入 PostgreSQL。
- 如果未来改用 PostgreSQL，必须重新设计并单独批准：为状态写入和读取创建限定到专用 Schema/表的最小权限角色，不授予 owner 权限，不复用现有 owner、GitHub、SMTP 或其他 Credential。

## 安全边界

- 生产器不得导入或调用现有完整健康检查中的模型、聊天、代理出口函数。
- 网络能力只允许 Python 标准库的本地 TCP 建连；禁止 HTTP 客户端和外部域名。
- Docker 命令必须使用固定容器名和限定 `--format`，不得输出完整 inspect、环境变量或 Compose 配置文件标签。
- 所有来自 Docker、SQLite、日志和操作系统的自由文本都不得进入状态文件。
- n8n 只通过覆盖式只读子挂载读取 `/exchange/ops/aurora-gateway`；不得新增 Docker Socket、Aurora 数据库、项目目录或敏感目录挂载。子挂载只读验证失败时不得导入工作流。
- SMTP Credential 和收发邮箱只在 n8n 实例内人工绑定；仓库导出使用故障安全占位。
- 未获得单独批准前，不执行合成邮件、真实成功静默验证、工作流发布或激活。

## 测试策略

### Aurora 单元测试

使用 Python 标准库 `unittest`、临时目录和依赖注入，不连接 Docker、FNOS、网络或真实数据库。

最低覆盖：

1. 五项全部通过并生成严格 Schema v1；
2. 容器缺失和非运行状态；
3. 历史重启计数只进入详情，不改变 `PASS`；
4. Compose 项目标识、工作目录或挂载不匹配；
5. 任一本地 TCP 端口不可达；
6. SQLite 完整性失败、渠道缺失、Token 无效、临近到期和已过期；
7. 续期日志缺失、部分非法、过期、成功、跳过和失败；
8. 续期锁空闲，以及超过 55 分钟仍被占用时产生 `refresh_in_progress_overrun`；
9. 状态优先级 `FAIL > WARN > PASS`；
10. 输出不包含测试 Token、连接串、邮箱、Cookie、敏感路径、业务正文、任意原始异常或非白名单字符串；
11. 输出超过 16 KiB 时拒绝发布；
12. 父目录缺失或为符号链接时失败关闭；
13. 原子替换成功；写入、刷新或替换失败时保留旧文件；
14. 活动目录只产生一个滚动 `latest.json`，不产生历史状态或逐次日志；
15. `PASS/WARN/FAIL/生产器错误` 的退出码分别符合接口；
16. 模块中不存在模型、聊天、外部域名或 HTTP 请求入口。

每项生产代码都必须先有会因目标能力缺失而失败的测试，再写最小实现。

### Studio OS 工作流测试

待 Studio OS 当前工作完成并单独批准后再实施。最低覆盖：

1. 固定名称、默认未激活、`Asia/Shanghai` 和 05:15/17:15 双时段；
2. 只读取固定 `/exchange` 文件；
3. 仓库导出不含邮箱、Credential、实例 ID、Token 或密钥；
4. Node.js 执行校验 Code 节点，覆盖 `PASS` 静默、`WARN`、`FAIL`、缺失、过期、超前、过大、格式错误、未知检查和总体状态矛盾；
5. 合成入口带 `[TEST]` 且不写状态文件；
6. 工作流连接只允许异常分支进入邮件节点。

## 分阶段实施门禁

### 门禁 1：设计与计划

- 只在 Windows Aurora 仓库形成设计与实施计划；
- 不修改 Studio OS，不编码、不提交、不推送。

### 门禁 2：Aurora 本地实现

- 只新增生产器和测试并更新 Aurora 文档；
- 只运行本地模拟测试和静态检查；
- 不连接 FNOS，不写 Studio OS，不提交、不推送。

### 门禁 3：Studio OS 本地工作流候选

- 等其验证与汉化工作完成后，单独批准修改工作流导出、测试和必要文档；
- 不连接 FNOS，不导入、不发信、不提交、不推送。

### 门禁 4：FNOS 现场只读验证

- 单独批准后，只核对当前 Docker/Compose 版本、n8n 与 PostgreSQL 状态、现有读写父挂载、目标路径存在性、UID/GID、宿主机时区、现有 cron、备份覆盖范围和回退基线；
- 只运行静态或元数据检查，不创建目录、同步文件、重建容器、修改 Compose/cron、尝试写入、导入工作流或读取 Credential 正文；
- 现场事实与设计不一致时返回精确差异，停止部署。

### 门禁 5：FNOS 状态生产器与只读子挂载部署

- 单独批准创建目标目录、同步脚本、安装受管 cron 和发布首个脱敏状态；
- 变更前备份当前 crontab，失败时恢复；
- 受控更新 Compose 并只重建 n8n，验证父挂载仍为读写、Aurora 子挂载为只读，且哨兵写入失败无残留；
- 任一只读子挂载验证失败时立即恢复变更前 Compose 与 n8n，不操作工作流、Credential 或邮件。

### 门禁 6：n8n 未激活导入与验证

- 单独批准导入一个默认未激活的工作流；
- 人工绑定现有 SMTP Credential；
- 合成邮件和真实 `PASS` 静默验证分别获得批准；
- 不激活日程。

### 门禁 7：激活与观察

- 单独批准发布并激活 05:15/17:15 日程；
- 观察真实 `PASS` 静默和异常分支，不调用模型或真实聊天；
- 稳定后再分别决定文档提交、推送和 FNOS Git 对齐。

## 回退

- n8n 异常：先停用 Aurora 告警工作流；不得删除其他工作流或 Credential。
- 生产器异常：移除本次受管 cron 条目并恢复变更前 crontab；既有 04:17/16:17 续期任务保持不变。
- 状态文件或目录默认保留为诊断证据；删除需要独立批准。
- 回退不得停用、修改或删除既有 Runtime Backup Alert，也不得影响其 03:20 状态生产、04:15 检查、SMTP 绑定或历史证据。
- 除恢复本次 n8n Compose 变更所必需的单容器重建外，不以回退为理由重启或重建 Aurora、New API、Mihomo、MetaCubeXD、n8n 或 PostgreSQL；PostgreSQL 不得重建。

## 验收标准

- Aurora 单元测试和现有测试全部通过；
- Studio OS 工作流策略测试在其实施阶段全部通过；
- 两个仓库分别通过 `git diff --check`、敏感特征扫描和忽略规则检查；
- 状态 Schema、日程、路径和固定状态码在两个仓库中逐项一致；
- 未部署前只称为候选实现；未完成合成邮件与成功静默验证前不得声称告警链路可用；未激活前不得声称已自动监控。
