-- Migration 026: adapter_spec — records adapter specifications between
-- component pairs that need runtime bridging.
--
-- When check_pair returns 'adapter_needed', an adapter_spec row captures:
--   - The source and target capabilities
--   - The runtime bridge type (subprocess, http, mcp, type_transform, etc.)
--   - I/O type transformation details
--   - Generated skeleton code (filled by the generator)
--
-- This makes "adapter_needed" verdicts actionable — each one becomes
-- a concrete spec that the skeleton generator can turn into code.

CREATE TABLE adapter_spec (
    id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    target_id           UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    bridge_kind         TEXT          NOT NULL,
    source_runtime      TEXT          NOT NULL,
    target_runtime      TEXT          NOT NULL,
    adapter_hint        TEXT          NOT NULL DEFAULT '',
    io_transform        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    skeleton_code       TEXT,
    test_code           TEXT,
    status              TEXT          NOT NULL DEFAULT 'draft',
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT adapter_spec_bridge_valid
        CHECK (bridge_kind IN (
            'subprocess', 'http', 'mcp_client', 'type_transform',
            'pip_install', 'skill_invoke', 'generic'
        )),
    CONSTRAINT adapter_spec_status_valid
        CHECK (status IN ('draft', 'generated', 'reviewed', 'tested')),
    CONSTRAINT adapter_spec_pair_unique
        UNIQUE (source_id, target_id)
);

CREATE INDEX adapter_spec_source_idx ON adapter_spec(source_id);
CREATE INDEX adapter_spec_target_idx ON adapter_spec(target_id);
