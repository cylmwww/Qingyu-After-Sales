CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE config_snapshots (
    id text PRIMARY KEY,
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ingestion_jobs (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('running','completed','failed','interrupted')),
    config_id text NOT NULL REFERENCES config_snapshots(id),
    index_id uuid,
    error jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE TABLE knowledge_documents (
    document_key text PRIMARY KEY,
    shop_id text NOT NULL,
    kind text NOT NULL,
    stable_id text NOT NULL,
    version text NOT NULL,
    source_path text NOT NULL,
    source_text text NOT NULL,
    source_hash text NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (shop_id, kind, stable_id, version)
);
CREATE TABLE knowledge_indexes (
    id uuid PRIMARY KEY,
    shop_id text NOT NULL,
    knowledge_version text NOT NULL,
    config_id text NOT NULL REFERENCES config_snapshots(id),
    source_fingerprint text NOT NULL,
    embedding_contract jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('building','published','failed','withdrawn')),
    job_id uuid NOT NULL REFERENCES ingestion_jobs(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
ALTER TABLE ingestion_jobs ADD CONSTRAINT ingestion_index_fk FOREIGN KEY (index_id) REFERENCES knowledge_indexes(id);
CREATE TABLE knowledge_chunks (
    index_id uuid NOT NULL REFERENCES knowledge_indexes(id),
    id text NOT NULL,
    document_key text NOT NULL REFERENCES knowledge_documents(document_key),
    parent_id text,
    role text NOT NULL CHECK (role IN ('parent','child')),
    ordinal integer NOT NULL,
    section text NOT NULL,
    text text NOT NULL,
    embedding_text text NOT NULL,
    token_count integer NOT NULL,
    source_start integer NOT NULL,
    source_end integer NOT NULL,
    metadata jsonb NOT NULL,
    embedding vector(1024),
    PRIMARY KEY (index_id, id),
    FOREIGN KEY (index_id, parent_id) REFERENCES knowledge_chunks(index_id, id),
    CHECK ((role = 'parent' AND embedding IS NULL) OR (role = 'child' AND embedding IS NOT NULL))
);
CREATE TABLE knowledge_publications (
    shop_id text PRIMARY KEY,
    index_id uuid NOT NULL REFERENCES knowledge_indexes(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE request_traces (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    config_id text NOT NULL REFERENCES config_snapshots(id),
    index_id uuid REFERENCES knowledge_indexes(id),
    question text NOT NULL,
    filters jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('running','completed','failed','interrupted')),
    result jsonb,
    error jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    elapsed_ms double precision
);
CREATE TABLE trace_events (
    trace_id uuid NOT NULL REFERENCES request_traces(id) ON DELETE CASCADE,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trace_id, sequence)
);
CREATE TABLE model_calls (
    id uuid PRIMARY KEY,
    trace_id uuid REFERENCES request_traces(id) ON DELETE CASCADE,
    job_id uuid REFERENCES ingestion_jobs(id),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((trace_id IS NULL) <> (job_id IS NULL))
);
CREATE INDEX trace_events_lookup ON trace_events(trace_id, sequence);
CREATE INDEX request_traces_started ON request_traces(started_at);
CREATE INDEX knowledge_chunk_scope ON knowledge_chunks(index_id, role);
