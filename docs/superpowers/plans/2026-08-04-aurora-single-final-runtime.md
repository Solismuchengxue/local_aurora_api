# Aurora 单一最终运行时实施计划

**目标：** 将已验证的 Aurora 2.5.0 Session Token canary 收敛为唯一正式 `aurora`，并清除旧与 canary 运行物。

1. 先以回归测试固定生产 Compose 的 digest、UID/GID、只读挂载、服务密钥和禁用外部 Token 要求。
2. 更新健康状态测试，要求 New API 渠道只接受 `http://aurora:8080` 与 `.env` 中的 `AURORA_AUTHORIZATION`。
3. 修改生产 Compose、环境示例、健康脚本和正式文档；删除 canary 与旧续期专用品。
4. 运行全部单元测试、Compose 静态解析、Markdown 链接、Git ignore 和 diff 检查。
5. 经独立 Git 门禁提交/push，并用限定 bundle 快进 FNOS checkout。
6. 经 FNOS 门禁迁移非敏感配置和受保护凭据元数据，切换唯一正式容器与 New API 渠道。
7. 验证真实最小聊天、状态生产、n8n active 和单一运行时后，删除旧与 canary 运行物及其回退包。

实施不修改 Studio OS、SMTP、PostgreSQL、n8n 工作流内容或其他工作流。
