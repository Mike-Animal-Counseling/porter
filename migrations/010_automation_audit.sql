-- porter:no-transaction
-- Automation emits these actions; the in-memory backend did not expose
-- the missing enum values in the production database.
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'auto_approve';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'auto_merge';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'auto_archive';
