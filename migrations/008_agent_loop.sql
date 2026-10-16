-- 只追加新运行时状态，不改写旧任务或业务数据。
CREATE TABLE agent_loop_config_versions (
    id text NOT NULL, version text NOT NULL, fingerprint text NOT NULL,
    configuration jsonb NOT NULL, PRIMARY KEY(id,version)
);
CREATE TABLE agent_loop_states (
    task_id uuid PRIMARY KEY REFERENCES agent_tasks(id),
    revision integer NOT NULL, state_version integer NOT NULL,
    schema_version text NOT NULL, state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
