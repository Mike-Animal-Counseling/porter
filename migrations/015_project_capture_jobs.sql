-- Accepted native turns survive API restarts. The receipt remains after the
-- payload is cleared so delivery retries cannot recreate reviewed memories.
CREATE TABLE project_capture_jobs (
    client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    project_tag TEXT NOT NULL,
    event_key TEXT NOT NULL CHECK (event_key ~ '^[0-9a-f]{64}$'),
    harness TEXT NOT NULL CHECK (harness IN ('codex', 'claude', 'dsh')),
    payload TEXT CHECK (char_length(payload) BETWEEN 1 AND 1800),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    failures INTEGER NOT NULL DEFAULT 0 CHECK (failures BETWEEN 0 AND 5),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (client_id, project_tag, event_key)
);

CREATE INDEX project_capture_jobs_ready_idx ON project_capture_jobs (next_attempt)
    WHERE payload IS NOT NULL;
