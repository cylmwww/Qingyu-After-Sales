# BM25、融合召回与重排

本页说明如何配置知识召回与排序。默认不设置 `RETRIEVAL_CONFIG_PATH`，使用纯向量 Top20 召回、Top6 上下文种子。检索配置改变候选选择，不修改政策和业务事实。

## 选择预设

预设位于 `configs/retrieval/`：

| 文件 | 召回与融合 | 重排 |
| --- | --- | --- |
| `r1-vector.v1.0.0.json` | 向量 Top20，单路 `direct` | 关闭 |
| `r2-bm25.v1.0.0.json` | BM25 Top20，单路 `direct` | 关闭 |
| `r3-hybrid-rrf.v1.0.0.json` | 向量与 BM25 各 Top20，RRF 融合 | 关闭 |
| `r4-hybrid-rrf-rerank.v1.0.0.json` | 双路各 Top20，RRF 融合 | 最多 30 个候选 |

这些预设最终均取 Top6 种子。RRF 默认 `k=60`、双路权重 1:1；BM25 默认 `k1=1.2`、`b=0.75`。可通过新版本配置调整数量、权重、候选上限及业务过滤字段。

单路必须使用 `direct`；双路可使用 `rrf` 或显式指定先后顺序的 `ordered-merge`。程序拒绝无效组合，不自动猜测替代策略。

## 先准备 BM25

BM25 根据某个已完成索引的子片段构建，不重新生成向量。使用 BM25 或混合召回前，必须为对应索引和 BM25 配置准备产物：

```bash
docker compose exec backend python -m backend.cli indexes
docker compose exec backend python -m backend.cli build-bm25 --retrieval-config configs/retrieval/r3-hybrid-rrf.v1.0.0.json
```

未指定 `--index-id` 时使用默认索引；也可追加该参数指定实际 UUID。页面“知识与配置”提供对应索引的“构建 BM25”操作。无密钥演示的 Compose 命令需沿用其项目名和配置文件。

产物绑定知识与索引版本、原文和元数据指纹、分词及词典版本。改变分词、词典或 BM25 参数时新增 BM25 `index_version` 并重建；仅调整召回数量、融合或重排参数通常不需要重新向量化。

## 应用到请求

在“知识与配置”页选择召回预设并应用，检查有效配置。该方式影响新任务，进行中的任务沿用冻结配置。

也可在 `.env` 中设置服务默认值后重新创建后端：

```dotenv
RETRIEVAL_CONFIG_PATH=configs/retrieval/r3-hybrid-rrf.v1.0.0.json
```

```bash
docker compose up -d --force-recreate backend
```

单次 CLI 查询可独立指定预设：

```bash
docker compose exec backend python -m backend.cli query --retrieval-config configs/retrieval/r3-hybrid-rrf.v1.0.0.json --question "鞋盒丢了能退吗？"
```

索引管理、`--config` 与 `--index-id` 的用法见[分块与索引](index-management.md)。已在管理页保存过配置时，以页面显示的最终有效配置为准。

## 启用独立重排

R4 除了兼容索引与 BM25，还需要单独配置重排服务。以下均为占位值，完整 URL、模型和响应协议应与实际服务一致：

```dotenv
RERANK_MODEL_PROVIDER=your-rerank-provider
RERANK_MODEL_URL=https://your-rerank-provider.example/rerank
RERANK_MODEL_API_KEY=replace-with-your-local-key
RERANK_MODEL_ID=your-rerank-model
RERANK_TIMEOUT_SECONDS=15
```

`RERANK_MODEL_URL` 只填写完整 URL，不带 `POST` 前缀。凭证不会自动借用生成或 Embedding 角色。配置变更后重新创建后端，再应用 R4 预设。

`flat-results-v1` 读取顶层 `results`；`dashscope-v1` 使用对应的 `input/parameters` 与 `output.results` 结构。协议由检索预设的 `rerank.protocol` 指定，应按服务实际返回格式选择。

重排输出需为每个候选提供唯一、合法的索引与有限数值分数。缺项、重复、越界、NaN、超时或接口失败都会终止相应请求，不自动退回 R3 或模拟结果。`mock` 模式的重排是本地模拟，只用于演示流程。

## 理解检索结果

中文 BM25 使用 `zh-dictionary-bigram-v1` 和 `configs/lexicons/product-terms.v1.0.0.json`：先规范化文本，再进行词典匹配与中文单字、相邻双字分词；英文和数字串独立处理。

融合在指定索引内按 `chunk_id` 去重。RRF 累加各路 `weight / (k + rank)`，不直接相加 BM25 与余弦原始分数。不同版本或不同条件的片段即使正文相同，仍可能需要分别保留。

业务过滤仅使用明确提供的 `category_id`、`purchased_at`、`after_sales_applied_at`；改写文本不能生成可信过滤条件。时间未知时不会一律选择最新政策。店铺与权限限制独立执行，不由召回配置关闭。

## 排查与追踪

请求追踪可查看各路候选、过滤原因、融合贡献、重排名次和分数，以及最终 TopK 与预算淘汰。BM25 缺失或指纹不匹配时，先核对当前索引，再重新构建对应产物。

Top6 种子还会经过答案上下文预算，最终入模内容可能更少。排序分数与引用可回查都不能证明答案得到充分支持；上下文补充和生成约束见[答案配置](answer-configuration.md)。
