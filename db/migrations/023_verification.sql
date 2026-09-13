-- Migration 023 — Lightweight Verification (Phase 4a)
--
-- verification_run:       one verification attempt for a capability version.
--                         Records environment, commands executed, overall
--                         result, duration, and captured logs/artifacts.
--
-- verification_assertion: individual assertions within a run. Each assertion
--                         is a specific check (INSTALL_SUCCESS, BUILD_SUCCESS,
--                         IMPORT_SUCCESS, SMOKE_PASS, CONTRACT_PASS) with its
--                         own pass/fail result and captured output.

CREATE TABLE verification_run (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    environment           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    result                TEXT        NOT NULL DEFAULT 'pending',
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    duration_ms           INTEGER,
    logs                  TEXT        NOT NULL DEFAULT '',
    artifacts             JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reproducibility_hash  TEXT,
    triggered_by          TEXT        NOT NULL DEFAULT 'manual',
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT verification_run_result_valid
        CHECK (result IN ('pending', 'running', 'passed', 'failed', 'error', 'timeout'))
);

CREATE INDEX verification_run_version_idx
    ON verification_run(capability_version_id);
CREATE INDEX verification_run_result_idx
    ON verification_run(result);

CREATE TABLE verification_assertion (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    verification_run_id UUID        NOT NULL REFERENCES verification_run(id) ON DELETE CASCADE,
    assertion_kind      TEXT        NOT NULL,
    command             TEXT        NOT NULL DEFAULT '',
    exit_code           INTEGER,
    stdout              TEXT        NOT NULL DEFAULT '',
    stderr              TEXT        NOT NULL DEFAULT '',
    duration_ms         INTEGER,
    passed              BOOLEAN     NOT NULL DEFAULT FALSE,
    reason              TEXT        NOT NULL DEFAULT '',
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT verification_assertion_kind_valid
        CHECK (assertion_kind IN (
            'install_success',
            'build_success',
            'import_success',
            'smoke_pass',
            'contract_pass'
        ))
);

CREATE INDEX verification_assertion_run_idx
    ON verification_assertion(verification_run_id);
CREATE INDEX verification_assertion_kind_idx
    ON verification_assertion(assertion_kind);
