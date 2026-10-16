-- 管理页只保存预设引用，不保存凭证；请求继续冻结自己的配置。
CREATE TABLE runtime_profiles (
    shop_id text PRIMARY KEY,
    revision integer NOT NULL DEFAULT 1,
    selections jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
