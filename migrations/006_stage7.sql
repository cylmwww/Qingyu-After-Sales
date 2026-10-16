CREATE TABLE review_config_versions (
 id text NOT NULL, version text NOT NULL, fingerprint text NOT NULL, configuration jsonb NOT NULL,
 PRIMARY KEY(id,version)
);
