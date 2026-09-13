-- Migration 022 — Symbol Intelligence (Phase 3)
--
-- capability_symbol: granular symbols (functions, classes, methods,
-- constants) exposed by a capability version. Richer than
-- capability_interface — includes module path, role classification,
-- docstring summaries, and qualified names for symbol-level search.
--
-- Scoped to Python first. Node/TypeScript deferred.

CREATE TABLE capability_symbol (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    evidence_item_id      UUID        REFERENCES evidence_item(id),
    module_path           TEXT        NOT NULL DEFAULT '',
    symbol_kind           TEXT        NOT NULL,
    symbol_name           TEXT        NOT NULL,
    qualified_name        TEXT        NOT NULL,
    signature             TEXT,
    return_type           TEXT,
    docstring_summary     TEXT,
    role                  TEXT        NOT NULL DEFAULT 'utility',
    language              TEXT        NOT NULL DEFAULT 'python',
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT capability_symbol_kind_valid
        CHECK (symbol_kind IN (
            'function', 'async_function', 'class', 'method',
            'async_method', 'constant', 'module'
        )),
    CONSTRAINT capability_symbol_role_valid
        CHECK (role IN (
            'entry_point', 'utility', 'data_model', 'cli_command',
            'api_endpoint', 'decorator', 'factory', 'middleware',
            'exception', 'test_helper'
        ))
);

CREATE INDEX capability_symbol_version_idx
    ON capability_symbol(capability_version_id);
CREATE INDEX capability_symbol_kind_idx
    ON capability_symbol(symbol_kind);
CREATE INDEX capability_symbol_role_idx
    ON capability_symbol(role);
CREATE INDEX capability_symbol_name_lower_idx
    ON capability_symbol(lower(symbol_name));
CREATE INDEX capability_symbol_qname_lower_idx
    ON capability_symbol(lower(qualified_name));
