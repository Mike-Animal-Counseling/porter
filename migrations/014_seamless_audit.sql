-- porter:no-transaction
-- Events introduced by the switching/workspace modules.
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'harness_switch';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'writeback';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'semantic_merge';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'resolve_conflict';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'lifecycle_sweep';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'lifecycle_archive';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'contradiction_flag';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'workspace_create';
ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'workspace_invite';
