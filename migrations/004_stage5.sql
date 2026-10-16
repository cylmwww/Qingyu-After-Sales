CREATE TABLE answer_config_versions (
    id text NOT NULL,
    version text NOT NULL,
    fingerprint text NOT NULL,
    configuration jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);
