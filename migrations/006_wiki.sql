-- Porter migration 006: LLM Wiki layer (entity graph, prototype).
--
-- Flag-gated via PORTER_WIKI_ENABLED (default off). When enabled, approving
-- a memory extracts entities/topics/relations (porter/llm.py) into:
--
-- 1. wiki_entities: canonical entity rows, unique by normalized name.
-- 2. wiki_memory_entities: "memory M mentions entity E" (mention edges).
-- 3. wiki_edges: directed relation edges between entities, attributed to
--    the memory they were extracted from (co-occurrence in the stub).
--
-- Encrypted (E2EE) memories are never indexed -- the server is
-- ciphertext-blind, so the wiki layer skips them by construction (the
-- application enforces this; the schema cannot, hence the comment).

-- ------------------------------------------------------- entity catalog
CREATE TABLE IF NOT EXISTS wiki_entities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,          -- normalized (lowercased)
    kind        TEXT NOT NULL DEFAULT 'topic', -- topic|person|project|tool|place
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------- mention edges
CREATE TABLE IF NOT EXISTS wiki_memory_entities (
    memory_id   UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id   UUID NOT NULL REFERENCES wiki_entities(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS wiki_memory_entities_entity_idx
    ON wiki_memory_entities (entity_id);

-- ------------------------------------------------------- relation edges
CREATE TABLE IF NOT EXISTS wiki_edges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    src_entity_id   UUID NOT NULL REFERENCES wiki_entities(id) ON DELETE CASCADE,
    dst_entity_id   UUID NOT NULL REFERENCES wiki_entities(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL DEFAULT 'related',
    memory_id       UUID REFERENCES memories(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_entity_id, dst_entity_id, relation, memory_id)
);
CREATE INDEX IF NOT EXISTS wiki_edges_src_idx ON wiki_edges (src_entity_id);
CREATE INDEX IF NOT EXISTS wiki_edges_dst_idx ON wiki_edges (dst_entity_id);
