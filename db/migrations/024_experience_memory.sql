-- Migration 024 — Project Experience Memory (Phase 5)
--
-- Tracks what happens AFTER a component is recommended: did it get
-- used in a project? Did it succeed or fail? What was the outcome?
--
-- component_project_use:  Records that a project adopted a specific
--                         capability version, linking to the recommendation
--                         and verification run that justified it.
--
-- failure_event:          Records a failure that occurred while using a
--                         component in a project. Feeds into experience
--                         scoring and failure-driven replacement.
--
-- experience_score:       Aggregated experience score per (capability, project)
--                         pair. Updated as usage events accumulate.

CREATE TABLE component_project_use (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    capability_id         UUID        NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    capability_version_id UUID        REFERENCES capability_version(id) ON DELETE SET NULL,
    recommendation_id     UUID        REFERENCES recommendation(id) ON DELETE SET NULL,
    verification_run_id   UUID        REFERENCES verification_run(id) ON DELETE SET NULL,
    status                TEXT        NOT NULL DEFAULT 'active',
    adopted_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at            TIMESTAMPTZ,
    retirement_reason     TEXT,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT component_project_use_status_valid
        CHECK (status IN ('active', 'retired', 'replaced', 'failed'))
);

CREATE INDEX component_project_use_project_idx
    ON component_project_use(project_id);
CREATE INDEX component_project_use_capability_idx
    ON component_project_use(capability_id);
CREATE INDEX component_project_use_status_idx
    ON component_project_use(status);

CREATE TABLE failure_event (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    component_use_id      UUID        NOT NULL REFERENCES component_project_use(id) ON DELETE CASCADE,
    failure_kind          TEXT        NOT NULL,
    severity              TEXT        NOT NULL DEFAULT 'error',
    summary               TEXT        NOT NULL DEFAULT '',
    detail                TEXT        NOT NULL DEFAULT '',
    occurred_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at           TIMESTAMPTZ,
    resolution            TEXT,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT failure_event_kind_valid
        CHECK (failure_kind IN (
            'install_failure',
            'import_failure',
            'runtime_error',
            'performance_degradation',
            'security_vulnerability',
            'api_breaking_change',
            'dependency_conflict',
            'build_failure',
            'test_failure',
            'other'
        )),
    CONSTRAINT failure_event_severity_valid
        CHECK (severity IN ('warning', 'error', 'critical'))
);

CREATE INDEX failure_event_use_idx
    ON failure_event(component_use_id);
CREATE INDEX failure_event_kind_idx
    ON failure_event(failure_kind);
CREATE INDEX failure_event_unresolved_idx
    ON failure_event(component_use_id)
    WHERE resolved_at IS NULL;

CREATE TABLE experience_score (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    capability_id     UUID        NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    total_uses        INTEGER     NOT NULL DEFAULT 0,
    total_failures    INTEGER     NOT NULL DEFAULT 0,
    success_rate      REAL        NOT NULL DEFAULT 1.0,
    last_failure_at   TIMESTAMPTZ,
    last_success_at   TIMESTAMPTZ,
    score             REAL        NOT NULL DEFAULT 0.0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT experience_score_unique
        UNIQUE (project_id, capability_id),
    CONSTRAINT experience_score_rate_valid
        CHECK (success_rate >= 0.0 AND success_rate <= 1.0)
);

CREATE INDEX experience_score_project_idx
    ON experience_score(project_id);
CREATE INDEX experience_score_capability_idx
    ON experience_score(capability_id);
CREATE INDEX experience_score_score_idx
    ON experience_score(score DESC);
