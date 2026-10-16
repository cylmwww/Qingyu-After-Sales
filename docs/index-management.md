# 分块配置与索引管理

知识索引把政策和 FAQ 转换为可检索片段，同时保留原文、来源、版本与父子关系。本页说明如何选择分块配置、构建新索引和切换默认索引。基础环境见[运行说明](setup.md)。

## 认识索引与配置

- **知识版本**指定一组政策与 FAQ，不等于向量索引的 ID。
- **索引 UUID**标识一次具体构建，绑定知识、分块、Embedding 和原文范围。
- **索引配置**位于 `configs/indexes/`，只保存分块与向量空间参数；服务地址和密钥从环境读取。
- **默认选择**决定未指定 `index_id` 的新请求使用哪个索引，不能改变已经运行的任务快照。

| 预设文件 | 分块方式 | 目标 / 上限 / 重叠 tokens |
| --- | --- | --- |
| `stage2-baseline.v1.0.0.json` | `rule-v1`，优先保留完整短条款 | 400 / 600 / 60 |
| `window-400.v1.0.0.json` | `token-window-v1`，按目标窗口拆分 | 400 / 600 / 60 |
| `window-200.v1.0.0.json` | `token-window-v1`，更短窗口 | 200 / 600 / 60 |

具体参数以文件为准。默认使用 `rule-v1`、`text-embedding-v4`、1024 维与精确余弦检索；父片段默认上限为 1200 tokens。编码前缀也占片段预算，实际正文长度可能小于配置目标。

短片段便于局部匹配，但可能拆开条件、结论与例外，也可能让同一条款的多个片段占满最终位置。完整短条款能一起保留这些信息；分块粒度应结合实际问题与上下文预算选择。

## 从页面管理

在“知识与配置”页填写访问令牌后，可以查看索引状态、文档来源、版本与入库任务，导入随项目提供的示例知识，并显式选择完成构建的兼容索引。

页面“导入示例知识”构建新索引，不自动切换默认选择；当前没有任意文件上传入口。BM25 是基于指定索引另行构建的派生产物，见[召回与排序](retrieval.md)。

## 用命令构建与查询

以下命令用于默认 Compose 环境。无密钥演示应在 `docker compose` 后补上启动时的 `-p qingyu-demo --env-file .env.mock.example`。

```bash
docker compose exec backend python -m backend.cli indexes
docker compose exec backend python -m backend.cli ingest --config configs/indexes/window-400.v1.0.0.json --no-publish
```

`--no-publish` 保留默认选择，构建完成后仍可通过 UUID 查询。真实模式构建会调用 Embedding；模拟模式生成模拟向量。

从输出取得 `index_id`，将下例全零 UUID 替换为实际值：

```bash
docker compose exec backend python -m backend.cli query --config configs/indexes/window-400.v1.0.0.json --retrieval-config configs/retrieval/r1-vector.v1.0.0.json --answer-config configs/answers/q0-baseline.v1.0.0.json --index-id 00000000-0000-0000-0000-000000000000 --question "定制商品的退货限制是什么？"
```

这里显式选择纯向量和基础回答预设，以免服务环境中的混合检索配置要求新索引尚未构建的 BM25 产物。`query` 执行单轮问答，不是多轮业务任务入口。

## 切换服务使用的索引

要让服务使用上述窗口配置，在 `.env` 中设置：

```dotenv
INDEX_CONFIG_PATH=configs/indexes/window-400.v1.0.0.json
```

在现有后端容器仍运行时，先用显式配置选择目标索引，再重新创建后端，使服务配置与索引保持一致：

```bash
docker compose exec backend python -m backend.cli select-index --config configs/indexes/window-400.v1.0.0.json --index-id 00000000-0000-0000-0000-000000000000
docker compose up -d --force-recreate backend
```

服务配置已与目标索引兼容时，也可通过页面选择。默认 `ingest` 在完整构建成功后切换默认索引；只有希望直接采用新索引时才省略 `--no-publish`。构建失败会保留旧默认索引，不覆盖原向量与原文。

## 兼容规则

模型、供应商地址、运行模式、维度、文档编码、查询编码、归一化、已声明模型修订与分块契约共同决定兼容性。同为 1024 维不表示来自同一向量空间。

默认文档编码为 `title-scope-rule-v1`，查询编码为 `raw-v1`，归一化为 `provider`；其他可选编码应通过新配置和新索引使用。配置文件覆盖对应环境参数，但接口地址和密钥仍由环境提供。

同一配置 `id + version` 的内容不能原地覆盖。修改策略时新增配置版本并构建新索引；旧索引与证据原文保留，以便回查历史请求。

| 常见错误 | 处理方向 |
| --- | --- |
| `embedding_index_incompatible`、`embedding_dimension_incompatible` | 使用与索引相同的向量空间配置，或重新构建。 |
| `chunk_index_incompatible` | 核对分块配置与所选索引。 |
| `index_config_version_conflict` | 为配置修改分配新版本。 |
| `chunk_payload_budget_incompatible` | 检查目标、重叠、上限与编码前缀预算。 |
| `knowledge_index_unavailable` | 检查索引是否存在、完整且可用。 |

父片段关系用于保留可回溯上下文；是否在回答时补充父片段，由[答案上下文配置](answer-configuration.md)控制，不是建立父子关系后自动拼入全部原文。
