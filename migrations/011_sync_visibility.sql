-- Preserve authorization metadata after hard deletion. Unknown legacy
-- tombstones remain NULL and fail closed for scoped consumers.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS scope_tags TEXT[];
UPDATE change_log c SET scope_tags = m.scope_tags
FROM memories m WHERE c.memory_id = m.id AND c.scope_tags IS NULL;

CREATE OR REPLACE FUNCTION capture_change_scope() RETURNS trigger AS $$
BEGIN
    IF NEW.scope_tags IS NULL THEN
        SELECT scope_tags INTO NEW.scope_tags FROM memories WHERE id = NEW.memory_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER change_log_capture_scope
    BEFORE INSERT ON change_log
    FOR EACH ROW EXECUTE FUNCTION capture_change_scope();
