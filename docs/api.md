# API 与操作确认

默认前缀 `/api/v1`；完整 Schema 由运行中的 `/docs` 提供。响应头包含 `x-trace-id` 与 `x-request-id`。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/health` | 进程存活 |
| GET | `/api/v1/ready` | 数据库可用性及依赖状态 |
| GET | `/api/v1/capabilities` | 实际运行后端与降级状态 |
| POST | `/api/v1/chat` | 返回完整对话结果 |
| POST | `/api/v1/chat/stream` | SSE 事件流 |
| GET | `/api/v1/admin/tools` | 工具列表 |
| GET | `/api/v1/admin/skills` | 技能列表 |
| GET | `/metrics` | Prometheus 指标 |

## 对话

```bash
curl http://127.0.0.1:8002/api/v1/chat -H 'Content-Type: application/json' -d '{"message":"电子发票多久能开？","user_id":"u_demo"}'
```

Windows PowerShell 可使用 `curl.exe` 或 `Invoke-RestMethod`。上面的 JSON 只包含演示数据。

| 请求字段 | 含义 |
| --- | --- |
| `message` | 非空文本，最多 4000 字符 |
| `user_id` | 用户标识，默认 `anonymous`；当前仅为演示身份，尚未认证 |
| `session_id` | 继续已有会话时传入 |
| `confirm_action_id` | 确认上次返回的待执行操作 |
| `idempotency_key` | 可选幂等键 |

主要响应包含 `answer`、`session_id`、`trace_id`、`strategy`、`citations`、`pending_action`、`refused`、`token_usage` 和 `latency`。策略取值为 `direct`、`single`、`multi`、`agent`、`refuse`。业务拒答可能仍返回 HTTP 200，应同时检查响应内容。

## 写操作确认

先发送“取消订单 ORD10003”，保留响应的 `session_id` 与 `pending_action.action_id`。再次请求时携带同一用户、会话与确认 ID：

```json
{
  "message": "确认执行",
  "user_id": "u_demo",
  "session_id": "填写上次响应中的 session_id",
  "confirm_action_id": "填写上次响应中的 action_id"
}
```

确认 ID 是服务端生成的，不要自行拼接。待执行操作与用户、会话绑定，有效期 15 分钟且保存在进程内。服务重启、过期或请求落到其他实例后，需要重新发起操作。前端用确认卡片完成这一步。

## 流式响应

向 `/api/v1/chat/stream` 发送相同请求体。事件包括 `started`、`node`、`token`、`answer` 和 `done`。`token` 事件是完成忠实度检查后的答案分段，不是未经检查的模型原始输出；最终 `answer` 携带完整结果。`/chat` 中的 `stream` 字段不用于切换接口。

## 管理与错误

生产环境只有配置 `ADMIN_API_TOKEN` 并携带匹配的 `x-admin-token` 才能访问管理接口；开发环境未配 token 时允许访问。不要将开发环境直接暴露在公网。

系统错误使用 HTTP 4xx/5xx，响应包含 `error_code`、`message` 与 `trace_id`。常见状态包括请求校验失败 422、未授权 401、幂等冲突 409、限流 429 和依赖故障 502/503。利用 `trace_id` 查找服务日志。

返回[项目说明](../README.md)。
