-- porter:no-transaction
-- Bring the persistent backend up to date with the REST/MCP memory types.
ALTER TYPE memory_type ADD VALUE IF NOT EXISTS 'personal';
ALTER TYPE memory_type ADD VALUE IF NOT EXISTS 'project';
