-- A BIGSERIAL alone can publish seq N+1 before the transaction for seq N
-- commits. A reader would then acknowledge N+1 and never receive N.
-- This transactional counter serializes feed publication through commit.
LOCK TABLE change_log IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE porter_sync_sequence (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    value BIGINT NOT NULL
);
INSERT INTO porter_sync_sequence (singleton, value)
SELECT TRUE, COALESCE(MAX(seq), 0) FROM change_log;

CREATE OR REPLACE FUNCTION assign_change_sequence() RETURNS trigger AS $$
BEGIN
    UPDATE porter_sync_sequence SET value = value + 1
    WHERE singleton = TRUE RETURNING value INTO NEW.seq;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER change_log_assign_sequence
    BEFORE INSERT ON change_log
    FOR EACH ROW EXECUTE FUNCTION assign_change_sequence();
