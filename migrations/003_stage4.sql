-- BM25 为指定知识/子片段版本的完整派生产物，事务写入后才可查询。
CREATE TABLE bm25_indexes (
    id text PRIMARY KEY,
    index_id uuid NOT NULL REFERENCES knowledge_indexes(id),
    shop_id text NOT NULL,
    index_version text NOT NULL,
    contract_hash text NOT NULL,
    artifact_hash text NOT NULL,
    artifact jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(index_id, shop_id, index_version)
);
CREATE TABLE retrieval_config_versions (
    id text NOT NULL,
    version text NOT NULL,
    fingerprint text NOT NULL,
    configuration jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(id, version)
);
