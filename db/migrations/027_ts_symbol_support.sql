-- Migration 027 — TypeScript/JavaScript symbol support
--
-- Expands capability_symbol CHECK constraints to accept TS/JS-specific
-- symbol kinds (interface, type_alias, enum) and roles (component, hook,
-- guard, pipe, enum_type). Adds a language index for ecosystem filtering.

-- Drop and recreate symbol_kind constraint with TS additions
ALTER TABLE capability_symbol
    DROP CONSTRAINT capability_symbol_kind_valid;

ALTER TABLE capability_symbol
    ADD CONSTRAINT capability_symbol_kind_valid
        CHECK (symbol_kind IN (
            'function', 'async_function', 'class', 'method',
            'async_method', 'constant', 'module',
            'interface', 'type_alias', 'enum'
        ));

-- Drop and recreate role constraint with TS additions
ALTER TABLE capability_symbol
    DROP CONSTRAINT capability_symbol_role_valid;

ALTER TABLE capability_symbol
    ADD CONSTRAINT capability_symbol_role_valid
        CHECK (role IN (
            'entry_point', 'utility', 'data_model', 'cli_command',
            'api_endpoint', 'decorator', 'factory', 'middleware',
            'exception', 'test_helper',
            'component', 'hook', 'guard', 'pipe', 'enum_type'
        ));

-- Language index for ecosystem-scoped queries
CREATE INDEX IF NOT EXISTS capability_symbol_language_idx
    ON capability_symbol(language);
