# 安装与启动

以下命令在项目根目录执行，前端命令除外。需要 Python 3.11+；前端需要 Node.js `^20.19.0` 或 `>=22.12.0`。

## 本地环境

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

```bash
python -m pip install -e ".[dev]"
```

复制 `.env.example` 为 `.env`。第一次运行推荐设置：

```dotenv
APP_ENV=development
APP_NAME=qingyu-new
DATABASE_URL=sqlite+aiosqlite:///./data/local/app.db
LOCAL_EMBEDDING_ENABLED=false
CROSS_ENCODER_ENABLED=false
UNSTRUCTURED_ENABLED=false
DEEPSEEK_API_KEY=
```

这是配置片段，不是 shell 命令。空密钥使用离线模式；填写自己的 DeepSeek 密钥后启用生成式回答。不要把 `.env` 放入版本控制。

```bash
python -m scripts.seed_demo
```

种子命令可以重跑，会补充缺失的演示数据。`u_demo` 对应 `ORD10001`（已发货）、`ORD10002`（已签收）和 `ORD10003`（已支付未发货）。API 启动时自动初始化表和知识索引，不自动填充演示订单。

```bash
python -m uvicorn app.asgi:app --host 127.0.0.1 --port 8002
```

## 前端

另开终端：

```bash
cd frontend
```

```bash
npm ci
```

```bash
npm run dev
```

访问 `http://127.0.0.1:5173`。默认代理目标为 `http://127.0.0.1:8002`。前端 `.env.example` 提供 `VITE_API_BASE` 和 `VITE_DEMO_USER_ID`；独立域名部署需要在 API 网关配置允许的来源。

Windows 安装好依赖后，也可从根目录运行 `scripts/dev-start.ps1` 同时启动两个开发进程；默认 API 8002、前端 5173，该脚本关闭三个重型模型/解析开关。

## 检查运行状态

```bash
curl http://127.0.0.1:8002/api/v1/health
```

```bash
curl http://127.0.0.1:8002/api/v1/ready
```

```bash
curl http://127.0.0.1:8002/api/v1/capabilities
```

`health` 检查存活；`ready` 以数据库为硬依赖；`capabilities` 列出当前模型、缓存和检索后端。配置了密钥不等于已成功调用供应方，实际生成效果需要单独验证。

## 可选能力

| 能力 | 安装与配置 |
| --- | --- |
| 本地稠密召回 | 安装 `.[rerank]`，开启 `LOCAL_EMBEDDING_ENABLED`，默认模型 `BAAI/bge-small-zh-v1.5` |
| Cross-Encoder 精排 | 安装 `.[rerank]`，开启 `CROSS_ENCODER_ENABLED`，默认模型 `BAAI/bge-reranker-base` |
| PDF、Office、图片等解析 | 安装 `.[docs]` 并开启 `UNSTRUCTURED_ENABLED`；具体格式还依赖 Poppler、Tesseract 或 LibreOffice |
| PostgreSQL | `DATABASE_URL=postgresql+asyncpg://用户名:密码@地址:5432/qingyu_new` |
| Redis | 配置 `REDIS_URL`，用于缓存与限流 |
| Qdrant | 配置 `QDRANT_URL` 与 `QDRANT_COLLECTION`，向量维度须与模型一致 |
| 管理接口 | 配置 `ADMIN_API_TOKEN`，请求附带 `x-admin-token` |

完整变量及默认值以 `.env.example` 和 `app/config/settings.py` 为准。模型首次加载可能联网下载；未安装可选依赖或模型失败会走对应降级路径。

## 常见问题

- 页面提示服务未连接：检查 API 是否在 8002 端口运行，并核对 `VITE_API_BASE`。
- 订单不存在：执行种子命令，并使用 `u_demo`。政策问答不依赖种子订单。
- 确认操作过期：重新发起操作；确认记录仅在当前服务进程保存 15 分钟。
- 返回离线证据文本：检查是否配置了 `DEEPSEEK_API_KEY`；离线模式不会生成与真实模型相同的回答。
- 修改知识库未生效：重启 API 重新加载索引。

返回[项目说明](../README.md)，或继续阅读[API](api.md)与[部署说明](deployment.md)。
