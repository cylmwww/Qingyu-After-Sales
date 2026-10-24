# qingyu · 轻语

**面向电商服务场景的智能客服工作台。** 轻语接收用户问题，根据意图与复杂度选择处理路径，结合政策知识库和业务工具回答咨询，展示可核对的来源，并在执行取消订单等写操作前请求用户确认。

项目围绕一条完整链路组织：**提出问题 → 识别与路由 → 检索或调用工具 → 校验回答 → 确认与执行**。可以通过 Web 工作台连续对话，也可以通过 HTTP API 或终端客户端接入。

## 主要功能

| 功能 | 说明 |
|---|---|
| 意图识别与策略路由 | 根据问题选择直接回答、单轮检索、多轮检索、业务工具或拒答路径 |
| 知识检索 | 对政策文档进行解析、清洗和切块，支持 BM25、稠密向量召回、RRF 融合与可选 Cross-Encoder 精排 |
| 多条件政策问答 | 通过查询改写与有限轮次补检处理跨条款问题，结合来源生成回答 |
| 订单与售后工具 | 提供订单、物流、退款、退货、商品、优惠券、库存和转人工等 15 个业务工具 |
| 操作确认与权限 | 工具执行经过参数校验、权限检查和风险判断；需要确认的写操作先返回待执行提案 |
| 回答检查与护栏 | 检查输入边界、提示注入、引用与答案忠实度，支持自纠错和证据不足时拒答 |
| 会话与上下文 | 保存服务端会话，按 Token 预算执行分级压缩与摘要 |
| Web 工作台 | 提供流式回答、来源展示、业务确认卡片、本地会话搜索、深色模式与移动端布局 |
| 技能与工具扩展 | 按意图匹配售后、物流、取消订单和推荐技能，统一注册本地业务工具与适配工具 |
| 存储与观测 | 默认使用 SQLite 与进程内后端，可配置 PostgreSQL、Redis 和 Qdrant，提供结构化日志与 Prometheus 指标 |

生成式回答使用 DeepSeek，需要配置模型密钥。未配置时仍可启动工作台，使用规则路由、证据节选和演示业务数据体验流程。向量模型、精排或外部存储不可用时会按实现降级，实际后端可通过 `/api/v1/capabilities` 查询；离线回答的相关性与完整性有限，重要结论需要核对原文。

## 架构与执行流程

后端基于 Python、FastAPI 与 LangGraph，前端使用 React、TypeScript 和 Vite。开发时前后端分别启动；生产构建后，FastAPI 可以在同一服务中提供页面与 API。

```text
Web 工作台 / HTTP API / 终端客户端
                  │
                  ▼
             ChatService
        会话加载、请求上下文与限流
                  │
                  ▼
          LangGraph 对话状态图
        输入护栏 → 意图识别 → 复杂度路由
                  │
       ┌──────────┼──────────────┐
       ▼          ▼              ▼
   直接回答    知识检索        业务工具
             查询改写        任务规划
             混合召回        参数与权限检查
             融合与精排      执行或返回待确认操作
             按需补检              │
       └──────────┼──────────────┘
                  ▼
           回答生成与忠实度检查
                  ▼
            上下文压缩与会话保存
                  ▼
        答案、来源、待确认操作与追踪信息

拒答路径：输入护栏或策略路由 → 拒答 → 上下文整理
确认路径：用户确认 → 绑定用户与会话的操作校验 → 工具执行
持久化：  SQLite / PostgreSQL；检索、缓存与限流可使用外部后端
```

| 路径 | 适用问题 | 处理方式 |
|---|---|---|
| `direct` | 问候与闲聊 | 直接生成回答，不进行知识检索 |
| `single` | 单一政策与常见问题 | 执行一轮知识检索，结合证据回答 |
| `multi` | 多条件、跨条款问题 | 改写查询、组合检索，并按需补充证据 |
| `agent` | 订单、物流与售后操作 | 规划任务并调用受权限约束的工具 |
| `refuse` | 越界、注入或无法可靠处理的问题 | 拒答或引导其他处理方式 |

检索层提供证据，工具层读取或修改业务数据，回答节点组织可展示文本。流式接口在答案完成检查后发送文本分段，最终结果包含 `session_id`、`trace_id`、`strategy`、`citations` 和可选的 `pending_action`。

## 代码目录与入口

```text
app/
├── asgi.py                # ASGI 启动入口
├── main.py                # 应用工厂、生命周期与前端静态资源服务
├── config/settings.py     # 环境变量与 .env 配置
├── api/routes/            # 对话、健康检查与管理接口
├── services/              # 组件装配、对话协调与演示数据
├── agents/graph.py        # LangGraph 状态图与路径连接
├── agents/nodes/          # 意图、规划、执行、生成和检查节点
├── retrieval/             # 文档处理、复杂度路由、召回、融合与精排
├── tools/                 # 业务工具、注册表、权限与参数校验
├── skills/definitions/    # 按业务场景组织的 JSON 技能定义
├── mcp/                   # 本地工具目录与远程 HTTP 调用适配
├── providers/             # DeepSeek、离线回答、Embedding 与重排后端
├── repositories/          # 数据库模型、订单、会话与幂等记录
├── memory/                # Token 预算、上下文压缩与摘要
├── guardrails/            # 输入边界、提示注入与忠实度检查
├── observability/         # 日志、请求上下文与 Prometheus 指标
└── cli/                   # 终端对话客户端
frontend/                  # React 工作台与前端构建配置
knowledge/                 # 电商政策知识库
scripts/                   # 知识导入、演示数据与开发启动脚本
eval/                      # 评测入口、指标与数据集
tests/                     # 真实 HTTP 场景与解析、融合算法单元测试
docs/                      # 安装、接口与部署说明
.env.example               # 配置项与默认值示例
Dockerfile                 # 前后端应用镜像
docker-compose*.yml        # 演示、单机与多实例组合配置
```

理解代码时，可以从 `asgi.py → main.py → services/container.py` 查看服务如何启动，再沿着 `api/routes/chat.py → services/chat_service.py → agents/graph.py` 查看一次对话如何完成。检索策略在 `retrieval/complexity_router.py`，业务工具在 `tools/factory.py` 统一注册。

## 快速开始

需要 Python 3.11+，前端需要 Node.js `^20.19.0` 或 `>=22.12.0`。下面的命令在项目根目录的 PowerShell 中执行。

### 1. 安装依赖

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

```powershell
npm --prefix frontend ci
```

### 2. 配置运行环境

复制环境模板：

```powershell
Copy-Item .env.example .env
```

在 `.env` 中按需配置自己的 DeepSeek 密钥。第一次体验可以关闭本地模型和额外解析组件，使用轻量模式：

```dotenv
APP_ENV=development
APP_NAME=qingyu-new
DEEPSEEK_API_KEY=
LOCAL_EMBEDDING_ENABLED=false
CROSS_ENCODER_ENABLED=false
UNSTRUCTURED_ENABLED=false
DATABASE_URL=sqlite+aiosqlite:///./data/local/app.db
```

`DEEPSEEK_API_KEY` 留空时使用离线回答，填写有效密钥后启用生成式回答。配置项见 [`.env.example`](.env.example)，修改后需要重启 API。`.env` 已被 Git 忽略，密钥不应写入公开文件。

本地稠密召回与 Cross-Encoder 精排需要安装 `.[rerank]` 并开启对应选项；PDF、Office 等解析需要安装 `.[docs]` 及格式所需的系统依赖。首次加载模型可能下载权重，完整安装方式见[启动说明](docs/setup.md)。

### 3. 初始化并启动

创建演示订单：

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_demo
```

启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.asgi:app --host 127.0.0.1 --port 8002
```

另开一个 PowerShell 窗口，在项目根目录启动前端：

```powershell
npm --prefix frontend run dev
```

打开 [Web 工作台](http://127.0.0.1:5173/)。可以先问“定制商品过了七天，但有质量问题还能退吗？”，查看回答与来源；再问“帮我查一下 ORD10001 的物流进度”，读取演示订单。发送“取消订单 ORD10003”会返回确认卡片，确认后才执行取消。

演示用户为 `u_demo`。种子命令可重跑，会补充缺失的数据；API 启动时自动初始化表并加载知识库，不自动创建演示订单。默认数据库为项目目录下的 `data/local/app.db`。

健康检查入口为 [`/api/v1/health`](http://127.0.0.1:8002/api/v1/health)，API 文档位于 [`/docs`](http://127.0.0.1:8002/docs)。`/api/v1/ready` 检查数据库就绪状态，`/api/v1/capabilities` 展示当前实际选用的后端。

### 通过 API 或终端使用

在 PowerShell 中发送对话请求：

```powershell
$response = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8002/api/v1/chat -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes((@{message='电子发票一般多久可以开具？'; user_id='u_demo'} | ConvertTo-Json)))
```

```powershell
$response
```

继续同一段对话时传入返回的 `session_id`。确认写操作时，还需传入上一条响应中的 `pending_action.action_id`，详见[接口与操作确认](docs/api.md)。

终端客户端连接已启动的 API：

```powershell
.\.venv\Scripts\qingyu-new.exe --base-url http://127.0.0.1:8002
```

## 知识库与业务工具

`knowledge/` 保存退换货、配送、支付、发票、优惠、保修和账户安全等政策文档。启动时加载文档并构建检索组件；修改知识文件后重启 API 重新加载。Markdown 可使用基础解析路径，其他格式依赖可选解析组件。

业务工具通过注册表提供统一的参数 Schema、执行入口和风险信息。规划器结合意图与匹配的技能选择工具；订单、物流与售后工具使用项目数据库中的演示数据，接入真实业务系统需要替换相应的数据访问实现。

`skills/definitions/` 中的 JSON 文件描述退货处理、订单跟踪、订单取消与商品推荐流程。当前按触发词或意图匹配一个技能，为规划提供业务约束。`mcp/` 提供本地营业时间、仓库截单示例与远程 HTTP 工具适配；远程接口需要符合项目的调用约定，具体实现见 [`app/mcp/runtime.py`](app/mcp/runtime.py)。

写操作先校验参数与权限，再按风险决定是否生成待确认提案。确认请求需要匹配用户、会话与操作 ID。待确认状态仅在当前服务进程中保留 15 分钟，重启、过期或请求落到另一实例后需要重新发起操作。

## 常用接口

默认业务前缀为 `/api/v1`。请求响应包含追踪信息，系统错误使用 HTTP 4xx/5xx 和统一错误体；业务拒答可能返回 HTTP 200，应同时检查响应内容。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/v1/health` | 检查进程存活 |
| `GET` | `/api/v1/ready` | 检查数据库与依赖状态 |
| `GET` | `/api/v1/capabilities` | 查询实际后端与降级状态 |
| `POST` | `/api/v1/chat` | 获取完整对话结果 |
| `POST` | `/api/v1/chat/stream` | 获取 SSE 对话事件流 |
| `GET` | `/api/v1/admin/tools` | 查看已注册工具 |
| `GET` | `/api/v1/admin/skills` | 查看技能定义 |
| `GET` | `/metrics` | 获取 Prometheus 指标 |

生产环境的管理接口需要配置 `ADMIN_API_TOKEN`，并在请求中携带 `x-admin-token`。当前业务请求中的 `user_id` 尚未绑定可信登录身份；对外服务前需要接入认证，并由服务端绑定用户身份。

## 运行配置

配置由 [`app/config/settings.py`](app/config/settings.py) 统一读取，完整模板见 [`.env.example`](.env.example)。

| 配置 | 默认值或用途 |
|---|---|
| `APP_ENV` / `APP_NAME` | `development` / `qingyu-new` |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | 生成模型密钥；默认模型 `deepseek-chat` |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | 模型请求超时 30 秒，最大重试次数 3 |
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./data/local/app.db`；可配置 PostgreSQL |
| `REDIS_URL` | 配置后使用 Redis 缓存与限流；未配置时使用进程内实现 |
| `QDRANT_URL` / `QDRANT_COLLECTION` | 向量存储地址与集合，默认集合 `cs_knowledge` |
| `KNOWLEDGE_DIR` | 知识库目录，默认 `knowledge` |
| `LOCAL_EMBEDDING_ENABLED` | 默认开启，模型为 `BAAI/bge-small-zh-v1.5`；轻量体验可关闭 |
| `CROSS_ENCODER_ENABLED` | 默认开启，模型为 `BAAI/bge-reranker-base`；需要可选模型依赖 |
| `UNSTRUCTURED_ENABLED` | 默认开启；非 Markdown 格式解析需要可选依赖与系统组件 |
| `RETRIEVAL_TOP_K_SIMPLE` / `RETRIEVAL_TOP_K_COMPLEX` | 简单与复杂查询的召回数量，默认 3 / 8 |
| `MAX_RETRIEVAL_HOPS` | 多轮检索上限，默认 3 |
| `MAX_AGENT_STEPS` / `MAX_TOOL_CALLS` | 规划步数与工具调用上限，默认 8 / 6 |
| `TOKEN_BUDGET` | 上下文预算，默认 128000 |
| `ADMIN_API_TOKEN` | 管理接口令牌，生产环境需显式配置 |

浏览器会话列表与可见消息保存在当前浏览器的 `localStorage` 中，清除浏览器数据或切换设备后不会自动恢复。服务端待确认状态尚未跨进程共享，使用多实例前需要完善状态持久化或请求路由。

## 构建与部署

构建前端：

```powershell
npm --prefix frontend run build
```

产物位于 `frontend/dist/`。FastAPI 启动时检测到该目录后，会从 `/` 提供页面；开发模式仍可通过 Vite 单独运行前端。

仓库提供三种 Compose 配置：

| 配置 | 组成 |
|---|---|
| `docker-compose.yml` | 应用与 SQLite，关闭重型模型组件，用于轻量体验 |
| `docker-compose.single.yml` | 单 API、PostgreSQL 与 Redis |
| `docker-compose.multi.yml` | 双 API、PostgreSQL、Redis、Qdrant 与 Nginx |

启动默认演示环境：

```powershell
docker compose up --build -d
```

访问 `http://127.0.0.1:8000`。需要订单演示时执行：

```powershell
docker compose exec api python -m scripts.seed_demo
```

单机与多实例配置还需要设置数据库密码，并按需配置模型。持久化、部署步骤和运行边界见[部署说明](docs/deployment.md)。

## 测试与检查

后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

静态检查：

```powershell
.\.venv\Scripts\python.exe -m ruff check app scripts tests
```

前端测试：

```powershell
npm --prefix frontend test
```

后端场景通过真实应用工厂与生命周期运行，使用临时 SQLite、仓库知识和演示订单，显式关闭外部服务与重型模型；模型协议场景仅连接本机 HTTP 替身。单元测试验证 JSON 解析、RRF 融合、缓存、压缩、模型适配与文档处理等独立边界；前端测试覆盖 SSE 解码与回答展示，不替代完整功能覆盖。运行产物、覆盖矩阵与尚未验证的范围见 [测试说明](tests/README.md)。真实模型效果、外部存储和完整浏览器交互仍需单独验证。

