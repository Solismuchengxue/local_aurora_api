# WorkBuddy 自定义模型踩坑指南

> 本文记录 2026-07-26 对 WorkBuddy、本地 New API 网关和 Aurora 活端点的实际排查结论。架构与通用能力边界见[飞牛 fnOS 部署指南](fnos_deployment.md#91-活端点能力实测)。

配置文件位于 `%USERPROFILE%\.workbuddy\models.json`，当前 WorkBuddy 使用顶层数组格式，不是 `{ "models": [...] }`。

## 1. URL 必须包含 `/v1`

`vendor: "Custom"` 会在 `url` 后追加 `/chat/completions`。

- 错误：`http://<NAS_IP>:3000`
- 正确：`http://<NAS_IP>:3000/v1`
- 同样错误：`http://<NAS_IP>:3000/v1/chat/completions`

如果只填到端口，实际请求会命中 New API 前端页面并收到 HTML。HTTP 状态可能仍是 200，但 WorkBuddy 无法解析，界面只显示“？”。

## 2. 两个模型都要关闭工具调用

`supportsToolCall: true` 会让 WorkBuddy 在每轮请求中附带大量工具定义。实测此类请求经过当前 Aurora 构建调用 `gpt-5-6-pro` 时，流式响应只有 usage 帧，`choices` 为空；去掉 `tools` 字段后恢复正常。

因此 `gpt-5-6-pro` 和 `gpt-5-6-thinking` 都必须设置：

```json
"supportsToolCall": false
```

代价是这两个自定义模型不能在 WorkBuddy 中调用读文件、运行命令等工具。纯对话和图片输入不受影响。这个限制需要由 Aurora 上游或网关兼容性修复，不能靠其他 `models.json` 开关绕过。

## 3. 出现“？”时先新建对话

旧会话历史如果包含触发安全拒绝的内容，后续消息可能持续返回“？”或 500，与当前模型配置无关。

识别线索：

- 日志出现 `stopReason: "refusal"`。
- 请求在 1–2 ms 内被标记为 `cancelled`。

遇到“？”时，先新建对话并测试 `1+1=?`。新对话正常，说明问题来自旧会话历史，不要继续修改模型配置。

## 4. 思考强度通过切换模型

当前 Aurora 构建不接受顶层 `reasoning_effort`，也不接受 Responses API 的 `reasoning.effort`，两种请求均返回 `422 Invalid conversation body`。

- `gpt-5-6-pro`：日常快速对话。
- `gpt-5-6-thinking`：复杂分析、代码和规划。

`gpt-5-6-thinking` 不会向第三方客户端返回可见的 `reasoning_content`。思考档表示答案质量更高，不表示可以查看思维链。

## 5. 图片生成仍隐藏，语音能力仅供 API 调用

`gpt-image-2` 已从当前 New API 渠道、默认令牌模型范围和 abilities 中隐藏，因此不会出现在鉴权后的 `GET /v1/models` 结果中。只有在 Aurora 升级后的受控复测期间才临时重新注册。

2026-08-04 已完成 New API rc.23 生产升级和一次 14 项真实矩阵。图片生成和编辑
通过，但图片变体失败，因此 passed-only 门禁仍隐藏整个 `gpt-image-2`。`gpt-4o`
因流式、Files 和组合翻译失败而隐藏；`tts-1` 因媒体完整性失败而隐藏。

New API 的渠道模型测试可能把 `gpt-image-2` 显示为成功，但这不是完整出图验收。当前限制来自图片端点之间的能力不一致，不能通过 WorkBuddy 模型开关解决。

WorkBuddy 的 `supportsImages: true` 只表示支持图片输入和看图，不表示这个端点能够生成图片。需要出图时使用 WorkBuddy 自身可用的图片生成能力。

`whisper-1` 是本轮唯一新增并保留的模型：生产入口已验证音频转写和原生翻译为英文。
它是 `/v1/audio/transcriptions` 与 `/v1/audio/translations` 的 API 模型，不是 WorkBuddy
聊天模型，不应加入下面的 `models.json`。英文音频转中文组合链路尚未通过。

## 6. 网页搜索可以使用

`gpt-5-6-pro` 和 `gpt-5-6-thinking` 经 Aurora 都能触发 ChatGPT 原生联网搜索，响应中可能出现 `turn0searchN` 引用标记。

WorkBuddy 通常使用自身的 WebSearch 工具，不依赖模型原生搜索；其他没有搜索工具的 OpenAI 兼容客户端可以直接受益于上游模型联网。引用透传偶有乱码或截断。

## 当前正确配置

`apiKey` 必须填写 New API 创建的客户端令牌，不要填写或分发 ChatGPT access token。

```json
[
  {
    "id": "gpt-5-6-pro",
    "name": "gpt-5-6-pro（快）",
    "vendor": "Custom",
    "url": "http://NAS_IP:3000/v1",
    "apiKey": "<NEW_API_TOKEN>",
    "supportsToolCall": false,
    "supportsImages": true,
    "supportsReasoning": false,
    "useCustomProtocol": false
  },
  {
    "id": "gpt-5-6-thinking",
    "name": "gpt-5-6-thinking（思考）",
    "vendor": "Custom",
    "url": "http://NAS_IP:3000/v1",
    "apiKey": "<NEW_API_TOKEN>",
    "supportsToolCall": false,
    "supportsImages": true,
    "supportsReasoning": true,
    "useCustomProtocol": false
  }
]
```

三项排障基线：

1. `url` 包含 `/v1`。
2. 两个模型的 `supportsToolCall` 都是 `false`。
3. 出现“？”时先新建对话测试。

## 凭据维护

- `models.json` 含明文 New API 令牌，不要提交、截图或外发。
- New API 渠道只使用稳定的 Aurora service key；ChatGPT Session Token 由 Aurora 内部换取和自然续期，失效告警后由用户更新受保护文件。
