# Aurora 单一最终运行时设计

日期：2026-08-04
状态：已批准，实施中

## 目标

把已验证的 Aurora 2.5.0 Session Token 方案从临时 canary 提升为唯一正式运行时。仓库当前树与 FNOS 都只保留一个可运行的 Aurora 拓扑，不保留旧 Aurora 或 canary 作为回退实例。

## 最终架构

- 唯一 Compose 项目为 `aurora-stack`，唯一 Aurora 服务和容器均命名为 `aurora`。
- Aurora 固定使用 `ghcr.io/aurora-develop/aurora@sha256:358533a8cd6355222297c699338fe6cdc024c6f3d951fb2fb03422350b9b7627`。
- 容器以 `65532:65532` 运行，把宿主机 `.secrets/session_tokens.txt` 只读挂载到 `/home/nonroot/session_tokens.txt`。
- `.env` 提供 `AURORA_AUTHORIZATION`；New API 渠道基座地址固定为 `http://aurora:8080`，渠道密钥与该服务密钥一致。
- `ENABLE_EXTERNAL_TOKEN=false`；Session Token 由 Aurora 启动时换取 Access Token，并由 Aurora 自身的后台健康检查负责自然续期。
- Aurora 继续仅通过 `mihomo:7890` 出站，Mihomo 保持既有新加坡节点策略。
- n8n 健康状态生产器只核对正式服务地址和正式 `.env` 的服务密钥；`refresh_log` 保留固定的 `refresh_not_applicable` 结果以兼容既有状态 Schema 和 n8n 工作流。

## 仓库收敛

- `docker-compose.yml` 是唯一可运行 Compose 入口。
- 删除两份 canary Compose、canary 环境示例、canary 探针、专用测试、报告契约和 canary 设计/计划文档。
- 删除旧的外部 Access Token 换新脚本及其专用测试；正式文档不再提供旧 cron 或旧回退路径。
- 保留并更新通用健康检查、n8n 状态生产器及其回归测试，使其只描述最终 Session Token 架构。
- Git 历史自然保留此前验证证据，不重写历史。

## FNOS 切换与清理

1. 先校验正式 Session Token 文件和正式环境文件的元数据与配置一致性，不输出凭据值。
2. 停止请求入口并移除旧 `aurora`；从唯一生产 Compose 创建最终 `aurora`。
3. 以比较并交换方式把 New API 渠道地址和服务密钥统一到最终值，然后只重启 New API。
4. 验证四个生产容器、Compose 标签、只读挂载、本地健康和一次脱敏真实 `gpt-4o` 请求。
5. 验证通过后删除两个 canary 容器/项目、canary 环境与凭据副本、闲置网络、无人使用的旧 Aurora 镜像和旧 Aurora/canary 回退包。
6. 移除旧 Access Token 刷新 cron，只保留健康状态生产 cron；重新发布并校验 `latest.json`。

不保留旧 Aurora 热回退。最终服务失败时停在故障现场继续修复，不恢复旧容器。

## 验收

- 仓库仅有 `docker-compose.yml` 一份可运行 Compose，全文无活动 canary 配置。
- FNOS 仅有一个名为 `aurora` 的 Aurora 容器，属于 `aurora-stack`，使用批准 digest、`65532:65532` 和只读 Session Token 挂载。
- New API 渠道基座为 `http://aurora:8080`，服务密钥只做布尔一致性检查。
- 四个生产容器健康；真实最小聊天返回 HTTP 200 且结构合法，不输出正文。
- 旧与 canary 容器、配置、凭据副本、cron 和无人使用镜像均不存在。
- `latest.json` 为脱敏 `PASS`，n8n 工作流保持 active，SMTP、PostgreSQL 和其他工作流不变。
