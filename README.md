# 青屿售后 · 证据问答

面向电商售后场景的 Agentic RAG 客服演示项目。它将政策知识、订单记录和只读业务工具连接到同一个对话工作台，让用户在提问、补充条件和追问时，获得带来源的解释，并能回查回答依据与执行过程。

项目围绕三个问题展开：适用哪条政策、当前业务记录是什么、回答依据是否充分。适合希望体验或了解知识问答、业务工具调用、多轮任务与证据审核如何协作的读者。

所有演示资料来自虚构的“青屿示例店”，包括政策、FAQ、客户、订单、售后和物流记录。项目不连接真实店铺，也不执行退款、取消订单或其他交易操作。

## 可以做什么

| 功能 | 使用方式与结果 |
| --- | --- |
| 政策与 FAQ 问答 | 查询退货条件、品类限制、包装要求等，查看引用原文、条款版本和来源。 |
| 订单与售后查询 | 查询模拟订单、售后退款进度与原订单出库物流；订单归属由服务端核对。 |
| 退款金额预估 | 根据已确认的商品单元和业务条件调用服务端计算，说明预估依据；预估不代表审批或到账。 |
| 多轮业务对话 | 对缺失条件发起澄清，支持补充或修正事实、切换订单、取消任务和显式恢复。 |
| 知识与索引管理 | 导入随项目提供的示例知识、构建并选择索引、查看来源和版本；页面不支持任意文件上传。 |
| 检索与回答配置 | 选择向量检索、BM25、融合召回、重排，以及查询改写、父条款和关联条款补充等预设。 |
| 可选证据检查与纠错 | 启用前置检查、后置审核及有限修复；需要审核的候选通过后才发布，未通过的稿件不作为最终回答。 |
| 请求追踪 | 查看检索排名、实际上下文、工具调用、审核过程、耗时、模型用量和错误。 |

默认使用纯向量检索与原问题，业务模式默认开启。查询改写、上下文补充、混合检索、重排、独立审核与纠错需要显式配置。复杂任务循环也是可选能力，默认使用 `legacy` 执行路径；启用循环需配套后置审核预设，且只接管动态任务路径。

## 页面与接口入口

默认启动后，浏览器访问以下地址：

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| 客服对话 | [打开工作台](http://127.0.0.1:5173/) | 提问、补充业务条件、查看引用与任务状态。 |
| 请求追踪 | [打开追踪页](http://127.0.0.1:5173/traces) | 查询请求并检查完整执行过程。 |
| 知识与配置 | [打开配置页](http://127.0.0.1:5173/knowledge) | 管理示例知识、索引与版本化预设。 |
| API 文档 | [打开接口文档](http://127.0.0.1:8000/docs) | 查看请求结构与接口定义。 |
| 健康检查 | [查看服务状态](http://127.0.0.1:8000/api/health) | 检查数据库与当前知识索引状态。 |

主要对话接口为 `POST /api/agent/chat`（多轮只读业务）和 `POST /api/chat`（单轮知识问答），均通过 SSE 返回事件。受保护接口使用 `Authorization: Bearer <API_ACCESS_TOKEN>`。

下方无密钥演示使用独立端口：前端 **5178**、后端 **8008**、数据库 **5438**。

## 快速开始

需要已运行的 Docker Engine 与 Docker Compose v2，并能下载依赖和容器镜像。以下命令均在项目根目录执行。

### 无模型密钥体验

使用仓库自带的模拟配置，无需创建或覆盖 `.env`：

```bash
docker compose -p qingyu-demo --env-file .env.mock.example up -d --build
docker compose -p qingyu-demo --env-file .env.mock.example ps
```

首次启动由 `init` 服务自动完成数据库迁移、示例数据检查和知识入库。打开 [模拟工作台](http://127.0.0.1:5178/)，在“本地访问令牌”中填写 `.env.mock.example` 的 `API_ACCESS_TOKEN`，然后发送问题。

模拟模式使用本地模拟向量、证据摘录与模拟业务响应，适合体验页面和执行流程，不代表真实模型的理解、生成或纠错效果。

停止演示并保留数据：

```bash
docker compose -p qingyu-demo --env-file .env.mock.example stop
```

### 接入真实模型

首次使用且不存在 `.env` 时，复制配置模板。PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Bash：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

编辑 `.env`，至少完成以下配置：

| 配置项 | 说明 |
| --- | --- |
| `RAG_MODE=real` | 使用真实模型接口。 |
| `API_ACCESS_TOKEN` | 自行设置的本地工作台访问令牌，浏览器填写同一值。 |
| `THINKING_MODEL_PROVIDER` / `BASE_URL` / `API_KEY` / `ID` | 对应完整的 `THINKING_MODEL_*` 变量，配置生成及 Agent 所用模型的供应商、兼容地址、密钥与模型 ID。 |
| `EMBEDDING_MODEL_PROVIDER` / `BASE_URL` / `API_KEY` / `ID` | 对应完整的 `EMBEDDING_MODEL_*` 变量，配置 Embedding 服务；模板使用 `text-embedding-v4`、1024 维。 |

模型 ID、接口地址和结构化响应协议需与供应商实际支持保持一致。重排服务仅在启用相关预设时需要独立配置。模型密钥只保存在服务端环境配置中，网页令牌框填写的是工作台访问令牌。

```bash
docker compose up -d --build
docker compose ps
```

首次启动会自动初始化并构建知识索引，真实模式会调用 Embedding 服务。服务就绪后打开 [客服工作台](http://127.0.0.1:5173/)，输入 `.env` 中的访问令牌即可使用。

模拟演示与上述默认启动使用不同的 Compose 项目及数据卷。真实与模拟索引不能混用；更换模型、模式或向量编码时需要构建兼容的新索引。

### 第一次可以这样体验

1. 在客服对话页询问“定制商品无理由退货有什么限制？”，展开回答引用，查看政策原文与适用条件。
2. 保持“只读业务模式”开启，询问“ORD-006 退款金额为什么不是 199 元？”，按提示补充已确认的条件。
3. 从回答进入请求追踪，查看检索材料、业务工具结果和模型调用；启用审核后还能查看检查与修复详情。
4. 在“知识与配置”页查看当前索引和预设。配置变更用于新任务，进行中及恢复的任务沿用原配置快照；使用 BM25 或混合检索前，先为对应索引构建 BM25。

## 工作原理

```text
政策 / FAQ → 校验与结构分块 → Embedding → PostgreSQL + pgvector

用户问题 → 会话与事实 → 意图识别与路径选择
                       ├─ 快速知识问答
                       ├─ 固定多步流程
                       └─ 动态规划与工具执行 / 可选复杂任务循环
                                ↓
                   知识检索 + 只读业务工具
                                ↓
                   条件澄清 / 回答生成 / 可选审核与有限修复
                                ↓
                         回答、引用与任务状态

请求配置、检索证据、工具结果和模型调用 → 持久化追踪与恢复
```

检索和业务处理共用证据链：政策保留来源、版本与父子片段，业务工具返回记录依据。模型负责理解、规划和解释，订单归属校验、业务计算、预算限制及发布检查由服务端执行。

前端使用 Vue 3、TypeScript、Vite、TDesign 与 Pinia；后端使用 FastAPI、LangGraph、PostgreSQL 与 pgvector。模型通过可配置适配层接入，检索可选 BM25、RRF 融合与独立重排。

## 代码架构

代码按页面交互、HTTP 接口、任务编排、检索与业务能力、持久化分工。入口和主要模块如下：

```text
frontend/src/
├─ main.ts                  页面路由与应用入口
├─ Chat.vue                 客服对话、澄清表单、引用与任务操作
├─ Traces.vue / TraceDetail.vue  请求列表与执行详情
├─ Knowledge.vue            知识索引与配置预设管理
└─ store.ts / sse.ts         会话状态、API 调用与事件流解析

backend/
├─ main.py                  FastAPI 入口、访问校验、对话与 SSE 接口
├─ management.py            知识、索引、配置与追踪管理接口
├─ cli.py                   初始化、入库、索引管理和查询命令
├─ config.py / *_config.py  环境配置与版本化策略
├─ agent_dispatch.py        按任务配置分派执行器
├─ agent.py                 LangGraph 路由、规划、执行、澄清与发布
├─ agent_loop/              可选的观察驱动任务循环
│  ├─ runtime.py            决策、工具、候选生成与复审循环
│  ├─ context.py / state.py 模型可见上下文与任务状态
│  ├─ tools.py / review.py  工具参数绑定与候选、审核校验
│  ├─ conditions.py         业务时间条件计算与依据绑定
│  ├─ requirements.py / obligations.py  证据需求与待关闭审核问题
│  ├─ budgeting.py / recovery.py / failures.py  预算、恢复与失败处理
│  └─ store.py              循环状态与发布持久化
├─ rag.py                   单轮检索、上下文组装与流式生成
├─ query.py / retrieval.py  查询改写、多查询与召回编排
├─ bm25.py / rerank.py      关键词检索与重排
├─ answer_context.py        父条款、关联条款补充及上下文预算
├─ business_tools.py        只读模拟业务、订单归属与退款预估
├─ evidence_bundle.py / evidence_review.py  证据组装与检查
├─ models.py / agent_model.py  模型协议适配与结构化调用
├─ knowledge.py / ingest.py 知识解析、分块与索引构建
└─ db.py / agent_store.py   数据库、会话、任务、事件与操作记录
```

核心调用链：

1. **单轮知识问答**：`store.ts → POST /api/chat → rag.py`，复用查询、召回、上下文与模型适配模块，通过 SSE 返回回答和引用。
2. **多轮业务任务**：`store.ts → POST /api/agent/chat → agent_store.py → agent_dispatch.py`，冻结任务配置并选择执行器。默认由 `agent.py` 负责路由与多步执行；启用复杂循环后，符合动态路径条件的任务交给 `agent_loop/runtime.py`，其余路径沿用原执行器。
3. **知识初始化**：Compose 的 `init → backend.cli seed`，完成迁移、检查点初始化和业务种子校验；没有可复用索引时，由 `ingest.py` 构建知识索引。页面管理操作通过 `management.py` 调用同一套入库与数据库能力。

`configs/` 保存策略预设，`data/` 保存示例知识与业务数据，`migrations/` 保存数据库迁移；`tests/` 保留数据库集成与任务链路回归。任务执行过程中使用冻结配置与持久化结果，恢复任务时不会直接套用刚修改的预设。

## 目录与使用文档

```text
backend/       API、检索、业务工具、任务执行与审核
frontend/      客服对话、请求追踪、知识与配置页面
configs/       索引、检索、回答、Agent、审核与循环预设
data/          示例政策、FAQ 与只读业务数据
migrations/    数据库迁移
docs/          配置与使用说明
compose.yaml   本地容器启动入口
```

按需要查阅：

- [工作台与无密钥启动](docs/workbench.md#无密钥部署)
- [本机运行与常见故障](docs/setup.md)
- [分块配置与索引管理](docs/index-management.md)
- [BM25、融合召回与重排](docs/retrieval.md)
- [查询改写、上下文与生成配置](docs/answer-configuration.md)
- [业务工具、多轮对话与任务恢复](docs/agent-and-sessions.md)
- [证据检查、答案审核与有限纠错](docs/evidence-review.md)
- [示例数据规范](docs/data-spec.md)与[数据清单](data/manifest.json)

## 使用边界

本项目用于本地演示与技术学习，业务记录始终为模拟数据。退款预估和政策解释不能替代真实售后审批，转人工状态也不表示已接入人工客服。项目不提供跨会话长期记忆、多个自治 Agent 或生产级多租户授权。

真实模型仍可能遗漏适用条件或作出错误解释，引用可回查及审核通过都不等于业务结论正确；真实业务语义与复杂纠错效果尚未完成完整人工验收。请勿直接用于真实售后裁定。

服务默认仅绑定本机回环地址。请求追踪会保存用户输入，使用示例数据体验即可；不要提交模型密钥、`.env` 或真实客户信息。
