-- Migration 025: inspection_run — auditable record of code inspections
--
-- Each time an extractor (manifests, licenses, interfaces, symbols,
-- secrets, tests) runs against a capability version's source snapshot,
-- an inspection_run row is created. This provides:
--   - Auditability: who inspected what, when, with which parser version
--   - Reproducibility: re-running with the same parser version + revision
--     should produce the same evidence
--   - Staleness detection: when was the last inspection relative to the
--     latest source revision?

CREATE TABLE inspection_run (
    id                      UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id   UUID          NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    extractor_name          TEXT          NOT NULL,
    extractor_version       TEXT          NOT NULL DEFAULT '1.0.0',
    source_revision         TEXT,
    started_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    finished_at             TIMESTAMPTZ,
    status                  TEXT          NOT NULL DEFAULT 'running',
    evidence_count          INTEGER       NOT NULL DEFAULT 0,
    error_detail            TEXT,
    metadata                JSONB         NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT inspection_run_status_valid
        CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT inspection_run_extractor_valid
        CHECK (extractor_name IN (
            'manifests', 'licenses', 'interfaces', 'symbols',
            'secrets', 'tests'
        ))
);

CREATE INDEX inspection_run_cv_idx
    ON inspection_run(capability_version_id, started_at DESC);

CREATE INDEX inspection_run_extractor_idx
    ON inspection_run(extractor_name, started_at DESC);
