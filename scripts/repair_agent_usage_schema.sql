-- 当数据库暂时无法执行 Flask-Migrate 时，可用本脚本补齐 Agent Usage 字段。
-- 所有语句均为幂等操作，重复执行不会覆盖已有数据。

ALTER TABLE message
    ADD COLUMN IF NOT EXISTS input_token_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_token_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cached_token_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reasoning_token_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS model_call_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tool_call_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS token_usage JSONB NOT NULL DEFAULT '{}'::jsonb;
