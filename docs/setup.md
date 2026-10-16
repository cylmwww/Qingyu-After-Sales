# 本机运行与常见故障

本页介绍青屿售后的安装、模型配置、启动和故障排查。首次体验可先使用[无密钥工作台](workbench.md#无密钥部署)，了解整体功能与代码结构可阅读[项目介绍](../README.md)。

应用提供政策问答、只读模拟业务查询、多轮任务、知识管理和请求追踪。所有店铺与业务数据均为虚构；真实模式中的“真实”只表示调用真实模型接口。

## 运行要求

容器方式需要已运行的 Docker Engine、Docker Compose v2，以及下载镜像和依赖的网络。宿主机不必另外安装 Python、Node.js 或 PostgreSQL。

直接在本机运行后端和前端时，需要 Python 3.12、Node.js 22、npm，以及启用 pgvector 的 PostgreSQL。仓库容器使用 Python 3.12 与 Node.js 22。

| 服务 | 默认地址或端口 | 无密钥演示端口 |
| --- | --- | --- |
| 前端 | http://127.0.0.1:5173 | 5178 |
| 后端 | http://127.0.0.1:8000 | 8008 |
| PostgreSQL | 127.0.0.1:5433 | 5438 |

以下命令在项目根目录执行。端口已占用时，可通过 `DB_PORT`、`BACKEND_PORT`、`FRONTEND_PORT` 调整容器映射。

## 配置真实模型

首次使用时创建 `.env`，已有文件直接编辑。PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Bash：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

按照供应商实际参数填写这些完整变量名：

| 变量 | 用途 |
| --- | --- |
| `RAG_MODE` | `real` 调用模型；`mock` 使用模拟向量、响应与证据摘录。 |
| `API_ACCESS_TOKEN` | 自行设置的本地工作台访问令牌；网页填写同一值。 |
| `THINKING_MODEL_PROVIDER`、`THINKING_MODEL_BASE_URL`、`THINKING_MODEL_API_KEY`、`THINKING_MODEL_ID` | 生成、意图识别、规划与结构化调用所用模型。 |
| `EMBEDDING_MODEL_PROVIDER`、`EMBEDDING_MODEL_BASE_URL`、`EMBEDDING_MODEL_API_KEY`、`EMBEDDING_MODEL_ID` | Embedding 服务；模板使用 `text-embedding-v4`。 |
| `EMBEDDING_MODEL_DIMENSIONS` | 向量维度，模板为 `1024`，须与供应商和索引一致。 |
| `STRUCTURED_RESPONSE_PROTOCOL` | 按模型实际响应选择 `strict-json-v1`、`json-or-sse-v1` 或 `sse-json-v1`。 |
| `GENERATION_ENABLE_THINKING` | 回答生成角色的思考开关，默认关闭，与其他角色独立。 |

`strict-json-v1` 要求非流式 JSON 正文；`json-or-sse-v1` 允许非流式请求返回完整 SSE；需要以流式请求取得结构化内容的接口使用 `sse-json-v1`。协议选择不放宽输出字段、引用或完整性校验。

网页访问令牌与模型密钥用途不同。模型密钥只填写在服务端环境配置中。重排服务的独立配置见[召回与排序](retrieval.md)。

## 使用 Docker Compose

```bash
docker compose up -d --build
docker compose ps --all
docker compose logs init
```

启动顺序为数据库、初始化服务、后端、前端。`init` 自动应用数据库迁移、初始化任务检查点、校验业务种子并准备知识索引；存在兼容索引时复用，没有时自动入库。因此首次正常启动不需要再手动执行 `migrate` 和 `ingest`。

真实模式初始化会调用 Embedding 服务；问答、审核与改写等调用也会产生供应商用量。打开[客服对话](http://127.0.0.1:5173/)，填写访问令牌，再询问“定制商品无理由退货有什么限制？”或“ORD-006 退款金额为什么不是 199 元？”。

修改 `.env` 后重新创建后端，确保新环境变量进入容器：

```bash
docker compose up -d --force-recreate backend
```

源码或依赖变化时使用 `docker compose up -d --build`。管理页保存的预设会参与运行配置，修改后可在“知识与配置”页检查最终有效值；已开始的任务保留原快照。

停止服务并保留数据库：

```bash
docker compose stop
```

无密钥演示使用独立项目名和配置，完整命令见[无密钥部署](workbench.md#无密钥部署)。后续查询状态、日志或停止服务时，应沿用启动时的项目名和 `--env-file`。

## 直接在本机运行

先准备 `.env`，填写本机数据库连接 `DATABASE_URL`。若使用 Compose 提供数据库，可按默认配置连接 `127.0.0.1:5433`。下面是 PowerShell 示例：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock
docker compose up -d db
.venv/Scripts/python.exe -m backend.cli seed
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

另一终端启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

Linux/macOS 将 `.venv/Scripts/python.exe` 替换为 `.venv/bin/python`。本机后端读取根目录 `.env`，前端代理 `/api` 到 `127.0.0.1:8000`。本机与容器后端应分别使用可用端口。

## 确认服务可用

访问[健康检查](http://127.0.0.1:8000/api/health)，检查 `status`、`mode`、`index_id` 和 `index_compatible`。`status=ok` 表示健康接口可访问，还应确认索引存在且兼容；它不证明模型接口或业务回答正确。

知识清单、来源和入库任务可在[知识与配置](http://127.0.0.1:5173/knowledge)中查看。离线数据检查可单独执行：

```bash
python -B scripts/validate_stage1.py
```

该检查无需数据库或模型密钥，范围见[数据校验说明](data-validation.md)。

## 常见故障

| 现象或错误 | 检查方式 |
| --- | --- |
| Docker 连接失败 | 确认 Docker Engine 已启动并就绪。 |
| 前端 401/403 | 核对页面令牌与 `API_ACCESS_TOKEN`；容器代理访问必须配置令牌。 |
| 初始化服务退出 | 查看 `docker compose logs init`，检查数据库权限、数据门禁与模型配置。 |
| `knowledge_index_missing` | 确认初始化或知识导入完成，并已有默认索引。 |
| `embedding_index_incompatible` 等兼容错误 | 核对模式、供应商地址、模型、维度和编码；使用匹配配置构建新索引。 |
| BM25 产物缺失或不匹配 | 为对应索引和召回配置先构建 BM25，再应用混合检索预设。 |
| 模型 401/429、连接失败或超时 | 检查服务端凭证、地址、供应商权限和限流；在追踪页查看具体失败角色。 |
| 结构化响应不合法或截断 | 检查响应协议和供应商输出上限；不完整结果不能视作已完成回答。 |
| 任务无法恢复 | 查看任务状态和冻结指纹；配置或源码不兼容、预算耗尽时需要结束旧任务。 |

真实与模拟向量不能混用；失败不会自动切换为模拟结果。项目默认只监听本机，访问令牌用于本地工作台保护，不能替代生产级多用户授权。
