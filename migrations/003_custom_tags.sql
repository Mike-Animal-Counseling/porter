-- 003: user-customizable scope_tags (PRD §10.3, panel settings).
-- Preset tags live in code (porter/oauth.py PRESET_SCOPE_TAGS); customs here.
CREATE TABLE IF NOT EXISTS custom_tags (
    tag         TEXT PRIMARY KEY CHECK (tag ~ '^[a-z0-9_]{1,32}$'),
    label       TEXT NOT NULL CHECK (char_length(label) BETWEEN 1 AND 64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
