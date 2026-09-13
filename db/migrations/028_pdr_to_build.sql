-- Migration 028 — PDR-to-Build workflow tables
-- Per vNext Implementation Spec §4.
--
-- Three additions:
--   requirement_source    — PDR provenance root
--   architecture_lock     — project-scoped, versioned, append-only
--   build_task cluster    — traceability links (task → requirement → verification)
--
-- Plus two nullable columns on project_requirement for source linking.

-- ---------------------------------------------------------------------
-- Addition A — requirement_source (PDR provenance root)
-- ---------------------------------------------------------------------
CREATE TABLE requirement_source (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    title         TEXT        NOT NULL,
    doc_kind      TEXT        NOT NULL DEFAULT 'pdr'
                    CHECK (doc_kind IN ('pdr', 'spec', 'change_request')),
    content_hash  TEXT        NOT NULL,
    raw_ref       TEXT,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, content_hash)
);

-- Link each requirement back to its source + the span it came from
ALTER TABLE project_requirement
    ADD COLUMN requirement_source_id UUID REFERENCES requirement_source(id),
    ADD COLUMN source_span JSONB;

ALTER TABLE project_requirement
    ADD COLUMN priority TEXT NOT NULL DEFAULT 'must'
        CHECK (priority IN ('must', 'should', 'could', 'wont')),
    ADD COLUMN acceptance_criteria TEXT;

-- ---------------------------------------------------------------------
-- Addition B — architecture_lock (versioned, append-only)
-- ---------------------------------------------------------------------
CREATE TABLE architecture_lock (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    version               INTEGER     NOT NULL,
    status                TEXT        NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'superseded')),
    approved_by           TEXT        NOT NULL,
    approved_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    approval_note         TEXT,
    decisions             JSONB       NOT NULL,
    rejected_alternatives JSONB       NOT NULL DEFAULT '[]'::jsonb,
    approved_providers    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    cost_commitments      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    unresolved_risks      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    source_lock_hash      TEXT        NOT NULL,
    UNIQUE (project_id, version)
);

-- At most one active lock per project
CREATE UNIQUE INDEX one_active_lock_per_project
    ON architecture_lock(project_id) WHERE status = 'active';

-- Append-only: never update a lock row, only supersede + insert new version
CREATE RULE architecture_lock_no_update_decisions AS
    ON UPDATE TO architecture_lock
    WHERE OLD.decisions IS DISTINCT FROM NEW.decisions
    DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------
-- Addition C — build_task + build_task_requirement + requirement_verification
-- ---------------------------------------------------------------------
CREATE TABLE build_task (
    id                           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                   UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    architecture_lock_id         UUID        NOT NULL REFERENCES architecture_lock(id) ON DELETE CASCADE,
    title                        TEXT        NOT NULL,
    objective                    TEXT        NOT NULL,
    status                       TEXT        NOT NULL DEFAULT 'planned'
                                   CHECK (status IN ('planned', 'in_progress',
                                                     'implemented', 'verified', 'abandoned')),
    permitted_paths              TEXT[],
    prohibited_changes           TEXT[],
    reused_capability_version_id UUID        REFERENCES capability_version(id),
    target_commit                TEXT,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX build_task_project_idx ON build_task(project_id);
CREATE INDEX build_task_lock_idx ON build_task(architecture_lock_id);

CREATE TABLE build_task_requirement (
    build_task_id          UUID NOT NULL REFERENCES build_task(id) ON DELETE CASCADE,
    project_requirement_id UUID NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    PRIMARY KEY (build_task_id, project_requirement_id)
);

CREATE TABLE requirement_verification (
    id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    build_task_id          UUID        REFERENCES build_task(id) ON DELETE SET NULL,
    outcome                TEXT        NOT NULL CHECK (outcome IN ('pass', 'fail', 'not_run')),
    evidence_ref           TEXT,
    verified_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX requirement_verification_req_idx
    ON requirement_verification(project_requirement_id);
CREATE INDEX requirement_verification_task_idx
    ON requirement_verification(build_task_id);
