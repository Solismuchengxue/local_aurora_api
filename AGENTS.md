# AGENTS.md

本文件适用于 `local_aurora_api` 项目。

## 项目边界

- 本项目维护飞牛 fnOS 上的 Docker Compose 部署配置与配套文档，不维护 Aurora、New API、Mihomo、MetaCubeXD 或 WatchCow 的上游源代码。
- 未经用户明确授权，不连接 NAS、不操作正在运行的容器、不修改防火墙或外部服务。
- 优先复用现有 Compose、配置和上游官方文档，不为简单部署需求新增依赖或脚本框架。
- WSL 只用于 Compose 和配置文件的静态检查；不得在本机 WSL 中启动或测试 Mihomo/Clash 代理。
- Win11 的 Clash Verge 属于本机既有代理环境，不与本项目的 NAS Mihomo 运行验证混用。

## 安全规则

- 不读取、打印、复制或提交 `.secrets/`、`.env`、`data/` 中的敏感内容。
- 令牌只允许做不会泄露正文的格式、到期时间或完整性检查。
- 修改或清理 `data/` 前必须先创建并验证备份。
- 不把 access token、订阅链接、New API 令牌或 `SESSION_SECRET` 写入示例、日志、提交或命令输出。
- `artifacts/` 中的第三方二进制默认仅本地保存；升级前核对来源、版本、架构和校验值。

## 文档职责

- `README.md` 只写项目介绍、安装、启动、使用方法和用户可见限制。
- `DESIGN.md` 记录正式架构、目录职责、关键边界和共享文档入口。
- `docs/fnos_deployment.md` 保存完整部署与排障步骤。
- `TODO.md` 保存当前行动和阻塞项，仅本地维护。
- `DEVLOG.md` 保存维护证据、踩坑和重要内部判断，仅本地维护。

当端口、镜像、卷、凭据流向或服务职责变化时，必须同步检查 `docker-compose.yml`、`README.md`、`DESIGN.md` 和部署指南。

## 修改原则

- 保持最小变更，不顺手升级镜像或重构无关文档。
- 已验证部署事实与候选方案必须明确区分。
- 不覆盖 `data/mihomo/config.yaml`；`config/mihomo/config.example.yaml` 只用于首次启动。
- 新建项目级多单词名称使用 `_`；上游品牌、镜像名和外部标识符保留原格式。

## 完成前验证

按变更范围执行：

1. 使用非敏感测试值运行 `docker compose config`。
2. 检查 Markdown 相对链接是否存在。
3. 运行 `git diff --check`。
4. 运行 `git check-ignore -v`，确认 `.env`、`.secrets/`、`data/`、`artifacts/*.fpk`、`TODO.md` 和 `DEVLOG.md` 被忽略。
5. 确认本地敏感文件和第三方安装包未跟踪、未暂存。
6. 涉及真实部署时，只在 NAS 上验证容器状态、日志、代理出口和 API 请求；未连接 NAS 时不得声称线上验证通过。
