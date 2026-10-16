CREATE SCHEMA IF NOT EXISTS agent_graph;
ALTER TABLE request_traces DROP CONSTRAINT request_traces_status_check;
ALTER TABLE request_traces ADD CONSTRAINT request_traces_status_check
 CHECK(status IN ('running','waiting','completed','failed','interrupted','cancelled'));
CREATE TABLE agent_config_versions (
 id text NOT NULL, version text NOT NULL, fingerprint text NOT NULL, configuration jsonb NOT NULL,
 PRIMARY KEY(id,version)
);
CREATE TABLE agent_sessions (
 id uuid PRIMARY KEY, shop_id text NOT NULL, customer_id text NOT NULL,
 facts jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent_tasks (
 id uuid PRIMARY KEY REFERENCES request_traces(id), session_id uuid NOT NULL REFERENCES agent_sessions(id),
 status text NOT NULL CHECK(status IN ('running','waiting','completed','failed','interrupted','cancelled')),
 attempt uuid NOT NULL, revision integer NOT NULL DEFAULT 0, question text NOT NULL,
 facts jsonb NOT NULL, frozen jsonb NOT NULL, business_hash text NOT NULL,
 snapshot_id text NOT NULL, budget jsonb NOT NULL, state jsonb NOT NULL DEFAULT '{}',
 lease_until timestamptz NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX agent_session_active ON agent_tasks(session_id)
 WHERE status IN ('running','waiting','interrupted','failed');
CREATE TABLE agent_submissions (
 session_id uuid NOT NULL REFERENCES agent_sessions(id), request_id uuid NOT NULL,
 fingerprint text NOT NULL, task_id uuid NOT NULL REFERENCES agent_tasks(id),
 PRIMARY KEY(session_id,request_id)
);
CREATE TABLE agent_operations (
 task_id uuid NOT NULL REFERENCES agent_tasks(id), revision integer NOT NULL, key text NOT NULL,
 kind text NOT NULL, attempt uuid NOT NULL, starts integer NOT NULL DEFAULT 1,
 arguments jsonb NOT NULL, result jsonb, status text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 PRIMARY KEY(task_id,revision,key)
);
