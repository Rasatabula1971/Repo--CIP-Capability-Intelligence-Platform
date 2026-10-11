-- Migration 030 — PDR-to-Build gap closure (Implementation Spec v1.1)
--
-- Closes the structural holes found in the v1.0 slice (migration 028):
--
--   A. requirement_source     — the PDR text is stored, so source quotes can
--                                be located and checked by CIP; revisions chain.
--   B. project_requirement    — kind (PDR requirement vs capability need),
--                                disposition, need search metadata, lifecycle.
--   C. requirement_need_link  — N:M between PDR requirements and the
--                                capability needs that satisfy them.
--   D. acceptance_criterion   — criteria as rows, so verification can point
--                                at one criterion.
--   E. requirement_search     — append-only search log. Proof a search ran,
--                                including a search that found nothing.
--   F. decision_record        — append-only decision log: what CIP computed,
--                                what was finally recorded, and who decided.
--   G. approval_request /
--      approval               — human gates with state, bound to the exact
--                                content approved (subject_hash).
--   H. audit_event            — the audit trail the v1.0 spec assumed existed.
--   I. architecture_lock      — approval link, payload hash, requirements
--                                hash, architecture doc hash, cross-cutting
--                                decisions; whole-row immutability.
--   J. requirement_verification — structured evidence for a pass.
--   K. evaluation             — evidence depth; six-verdict alignment.
--
-- Additive only. Legacy rows keep working: new columns are nullable or
-- defaulted, and verification rows written before this migration are
-- marked `legacy` so the pass-evidence CHECK never re-judges them.
--
-- House style note. Append-only tables use the same
-- `ON UPDATE ... DO INSTEAD NOTHING` rule as build_authorization (010).
-- DELETE stays allowed so project-level ON DELETE CASCADE keeps working.
-- Two places need conditional logic a rule cannot raise on, so they use
-- triggers that RAISE instead of swallowing the write: architecture_lock
-- immutability and requirement_need_link kind/project consistency.

-- ---------------------------------------------------------------------
-- A. requirement_source — store the text; chain revisions
-- ---------------------------------------------------------------------
--
-- raw_ref (from 028, never written by v1.0) now holds where the text came
-- from: the absolute file path, or 'inline' when the agent supplied the
-- text as an argument. source_file_hash is the sha256 of the file's raw
-- bytes, so a human can compare it with `Get-FileHash` / `sha256sum`;
-- content_hash is over the normalised text and will differ for a CRLF file.
--
-- activated_at decides which source is current: the most recently
-- activated one. Re-ingesting an earlier text re-activates its row, so a
-- PDR can be reverted. supersedes_id records what was current when a
-- source was first ingested.
ALTER TABLE requirement_source
    ADD COLUMN raw_text         TEXT,
    ADD COLUMN supersedes_id    UUID REFERENCES requirement_source(id) ON DELETE SET NULL,
    ADD COLUMN source_file_hash TEXT,
    ADD COLUMN activated_at     TIMESTAMPTZ;

UPDATE requirement_source SET activated_at = ingested_at;

ALTER TABLE requirement_source
    ALTER COLUMN activated_at SET NOT NULL,
    ALTER COLUMN activated_at SET DEFAULT now();

-- ---------------------------------------------------------------------
-- B. project_requirement — kinds, dispositions, need metadata, lifecycle
--
-- kind:
--   functional | non_functional | constraint — a requirement taken from
--       the PDR. Carries a source quote and acceptance criteria.
--   capability_need — a reusable capability the build needs (auth, job
--       queue, PDF render). This is the unit that is searched, fit-
--       evaluated and decided. Reuses fit_evaluation / recommendation /
--       build_authorization unchanged, because those FK to this table.
--
-- disposition (PDR requirements only; NULL = not yet dispositioned):
--   via_needs      — satisfied through linked capability needs
--   bespoke        — project-specific logic; nothing reusable to search for
--   not_applicable — not a build item (e.g. enforced as a project constraint)
--   deferred       — explicitly out of this build
-- ---------------------------------------------------------------------
ALTER TABLE project_requirement
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'functional'
        CHECK (kind IN ('functional', 'non_functional', 'constraint',
                        'capability_need')),
    ADD COLUMN disposition TEXT
        CHECK (disposition IN ('via_needs', 'bespoke', 'not_applicable',
                               'deferred')),
    ADD COLUMN disposition_reason TEXT,
    ADD COLUMN search_terms  TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN problem_types TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'retired')),
    ADD COLUMN retired_reason TEXT;

ALTER TABLE project_requirement
    ADD CONSTRAINT project_requirement_need_no_disposition
        CHECK (kind <> 'capability_need' OR disposition IS NULL),
    ADD CONSTRAINT project_requirement_disposition_reason
        CHECK (disposition IS NULL
               OR disposition = 'via_needs'
               OR length(trim(coalesce(disposition_reason, ''))) > 0),
    ADD CONSTRAINT project_requirement_retired_reason
        CHECK (status <> 'retired'
               OR length(trim(coalesce(retired_reason, ''))) > 0);

CREATE INDEX project_requirement_kind_idx
    ON project_requirement(project_id, kind);
CREATE INDEX project_requirement_problem_types_idx
    ON project_requirement USING GIN (problem_types);

-- A row's kind never changes: a requirement does not become a need.
CREATE FUNCTION project_requirement_kind_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.kind = 'capability_need') <> (NEW.kind = 'capability_need') THEN
        RAISE EXCEPTION
            'project_requirement %: kind cannot change between a PDR requirement and a capability_need',
            OLD.id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER project_requirement_kind_immutable_trg
    BEFORE UPDATE OF kind ON project_requirement
    FOR EACH ROW EXECUTE FUNCTION project_requirement_kind_immutable();

-- ---------------------------------------------------------------------
-- C. requirement_need_link — which needs satisfy which PDR requirements
-- ---------------------------------------------------------------------
CREATE TABLE requirement_need_link (
    requirement_id UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    need_id        UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (requirement_id, need_id),
    CONSTRAINT requirement_need_link_distinct CHECK (requirement_id <> need_id)
);

CREATE INDEX requirement_need_link_need_idx ON requirement_need_link(need_id);

CREATE FUNCTION requirement_need_link_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r_kind TEXT; r_proj UUID;
    n_kind TEXT; n_proj UUID;
BEGIN
    SELECT kind, project_id INTO r_kind, r_proj
      FROM project_requirement WHERE id = NEW.requirement_id;
    SELECT kind, project_id INTO n_kind, n_proj
      FROM project_requirement WHERE id = NEW.need_id;
    IF r_kind = 'capability_need' THEN
        RAISE EXCEPTION
            'requirement_need_link: % is a capability_need, not a PDR requirement',
            NEW.requirement_id;
    END IF;
    IF n_kind <> 'capability_need' THEN
        RAISE EXCEPTION
            'requirement_need_link: % is not a capability_need', NEW.need_id;
    END IF;
    IF r_proj <> n_proj THEN
        RAISE EXCEPTION
            'requirement_need_link: requirement and need belong to different projects';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER requirement_need_link_check_trg
    BEFORE INSERT OR UPDATE ON requirement_need_link
    FOR EACH ROW EXECUTE FUNCTION requirement_need_link_check();

-- ---------------------------------------------------------------------
-- D. acceptance_criterion — one row per criterion
-- ---------------------------------------------------------------------
CREATE TABLE acceptance_criterion (
    id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    ordinal                INTEGER     NOT NULL CHECK (ordinal >= 1),
    text                   TEXT        NOT NULL CHECK (length(trim(text)) > 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT acceptance_criterion_ordinal_unique
        UNIQUE (project_requirement_id, ordinal)
            DEFERRABLE INITIALLY DEFERRED
);

-- Backfill from the v1.0 single-blob column.
INSERT INTO acceptance_criterion (project_requirement_id, ordinal, text)
SELECT id, 1, acceptance_criteria
  FROM project_requirement
 WHERE acceptance_criteria IS NOT NULL
   AND length(trim(acceptance_criteria)) > 0;

-- ---------------------------------------------------------------------
-- E. requirement_search — append-only search log
--
-- One row per search_for_requirement call. This, not fit_evaluation, is
-- the proof that a search ran: a search that finds nothing writes a row
-- with found_count = 0, so an empty registry no longer blocks BUILD and
-- a BUILD no longer needs a junk candidate to unlock it.
--
-- need_fingerprint ties the search to the need definition that was
-- searched. Edit the need's terms or constraints and earlier searches
-- stop counting.
-- ---------------------------------------------------------------------
CREATE TABLE requirement_search (
    id                               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id           UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    query_source                     TEXT        NOT NULL
                                       CHECK (query_source IN ('need_terms', 'custom')),
    queries                          TEXT[]      NOT NULL CHECK (cardinality(queries) >= 1),
    filters                          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    need_fingerprint                 TEXT        NOT NULL,
    registry_size                    INTEGER     NOT NULL CHECK (registry_size >= 0),
    found_count                      INTEGER     NOT NULL CHECK (found_count >= 0),
    evaluated_capability_version_ids UUID[]      NOT NULL DEFAULT '{}',
    unevaluable_count                INTEGER     NOT NULL DEFAULT 0 CHECK (unevaluable_count >= 0),
    evaluation_slugs                 TEXT[]      NOT NULL DEFAULT '{}',
    -- True when a term returned as many hits as the search would take, so
    -- there may be matches it never looked at. "Nothing fits" cannot be
    -- concluded from a truncated search.
    truncated                        BOOLEAN     NOT NULL DEFAULT false,
    -- Hits removed by the project's own policy constraints before
    -- evaluation. Logged so that a filter set elsewhere cannot make an
    -- "unfiltered" search look empty.
    policy_excluded_count            INTEGER     NOT NULL DEFAULT 0 CHECK (policy_excluded_count >= 0),
    -- A fingerprint of everything in the registry a search reads. A decision
    -- is refused if it has moved since the sweep it rests on: registry_size
    -- alone does not move when an existing match is ingested or analysed.
    registry_stamp                   TEXT,
    searched_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX requirement_search_req_idx
    ON requirement_search(project_requirement_id, searched_at);

CREATE RULE requirement_search_no_update AS
    ON UPDATE TO requirement_search DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------
-- F. decision_record — append-only decision log
--
-- recommendation (009) stays the single mutable "current verdict" row
-- that Step 13 reads. decision_record is the history: one row per
-- record_decision call, never rewritten. The lock references
-- decision_record ids, which survive re-recommendation.
--
-- recommendation_id is deliberately NOT a foreign key: recommendation
-- rows are deleted and re-inserted on every re-run, and an ON DELETE
-- SET NULL action would collide with the no-update rule below.
--
-- computed_verdict is NULL when CIP could not compute one: the need has
-- no machine-checkable constraints, so fit_score is 1.0 for everything
-- and the rules would "decide" on text match alone.
-- ---------------------------------------------------------------------
CREATE TABLE decision_record (
    id                             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id         UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    recommendation_id              UUID,
    search_ids                     UUID[]      NOT NULL CHECK (cardinality(search_ids) >= 1),
    need_fingerprint               TEXT        NOT NULL,
    computed_verdict               TEXT
        CHECK (computed_verdict IN ('ADOPT', 'ADAPT', 'WRAP', 'REFERENCE', 'REJECT', 'BUILD')),
    computed_rule_name             TEXT,
    computed_capability_version_id UUID        REFERENCES capability_version(id),
    final_verdict                  TEXT        NOT NULL
        CHECK (final_verdict IN ('ADOPT', 'ADAPT', 'WRAP', 'REFERENCE', 'BUILD')),
    final_capability_version_id    UUID        REFERENCES capability_version(id),
    pinned_revision_key            TEXT,
    pinned_source_asset_key        TEXT,
    decided_by                     TEXT        NOT NULL
        CHECK (decided_by IN ('cip_rules', 'llm_override', 'llm_judgment')),
    coverage_outcome               TEXT        NOT NULL
        CHECK (coverage_outcome IN ('candidates_evaluated', 'found_but_unevaluable',
                                    'banked_only', 'no_registry_coverage')),
    flags                          TEXT[]      NOT NULL DEFAULT '{}',
    evaluation_slugs               TEXT[]      NOT NULL DEFAULT '{}',
    rationale                      TEXT        NOT NULL DEFAULT '',
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Insertion order. "Latest decision" is decided by this, not by
    -- created_at: now() is constant inside a transaction, so timestamps
    -- can tie.
    seq                            BIGINT      GENERATED ALWAYS AS IDENTITY,
    -- Same invariant recommendation_pin_required enforces: a reuse
    -- verdict names an exact revision.
    CONSTRAINT decision_record_pin_required
        CHECK (
            (final_verdict = 'BUILD' AND final_capability_version_id IS NULL)
            OR (final_verdict <> 'BUILD'
                AND final_capability_version_id IS NOT NULL
                AND pinned_revision_key IS NOT NULL
                AND pinned_source_asset_key IS NOT NULL)
        ),
    CONSTRAINT decision_record_override_needs_rationale
        CHECK (decided_by = 'cip_rules' OR length(trim(rationale)) > 0)
);

CREATE INDEX decision_record_req_idx
    ON decision_record(project_requirement_id, seq);

CREATE RULE decision_record_no_update AS
    ON UPDATE TO decision_record DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------
-- G. approval_request / approval — human gates with state
--
-- A request freezes the exact content put in front of the human
-- (subject) and its hash. A gate passes only if an approval exists for
-- the hash of the CURRENT content, so editing anything after approval
-- silently un-approves it.
--
-- Nothing in the MCP server writes to `approval`. The only writer is the
-- `cip-composer approvals` CLI, which demands an interactive terminal.
-- That is governed cooperation, not a sandbox: see the spec, section 9.
-- ---------------------------------------------------------------------
CREATE TABLE approval_request (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    kind         TEXT        NOT NULL
                   CHECK (kind IN ('requirements_confirmation', 'architecture_lock')),
    subject_hash TEXT        NOT NULL,
    subject      JSONB       NOT NULL,
    summary      TEXT        NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT approval_request_unique UNIQUE (project_id, kind, subject_hash)
);

CREATE RULE approval_request_no_update AS
    ON UPDATE TO approval_request DO INSTEAD NOTHING;

CREATE TABLE approval (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_request_id UUID        NOT NULL UNIQUE REFERENCES approval_request(id) ON DELETE CASCADE,
    outcome             TEXT        NOT NULL CHECK (outcome IN ('approved', 'rejected')),
    approver            TEXT        NOT NULL CHECK (length(trim(approver)) > 0),
    channel             TEXT        NOT NULL CHECK (channel IN ('cli_tty')),
    note                TEXT,
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE RULE approval_no_update AS
    ON UPDATE TO approval DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------
-- H. audit_event — append-only trail for the PDR workflow
-- ---------------------------------------------------------------------
CREATE TABLE audit_event (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID        REFERENCES project(id) ON DELETE CASCADE,
    event_type    TEXT        NOT NULL CHECK (length(trim(event_type)) > 0),
    actor         TEXT        NOT NULL DEFAULT 'mcp',
    subject_table TEXT,
    subject_id    TEXT,
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Insertion order. created_at is the transaction's START time: two
    -- calls that overlap can commit in the opposite order to their
    -- timestamps, and "the latest lock request" must mean the last one
    -- asked for, not the one whose transaction began last.
    seq           BIGINT      GENERATED ALWAYS AS IDENTITY
);

CREATE INDEX audit_event_project_idx ON audit_event(project_id, created_at);
CREATE INDEX audit_event_type_idx    ON audit_event(project_id, event_type);

CREATE RULE audit_event_no_update AS
    ON UPDATE TO audit_event DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------
-- I. architecture_lock — approval link, hashes, cross-cutting decisions
--
-- approval_id / payload_hash are NULL only on v1.0 locks, which were
-- created with a free-text approver and no approval record. The workflow
-- treats those as unapproved and asks for a relock.
-- ---------------------------------------------------------------------
ALTER TABLE architecture_lock
    ADD COLUMN approval_id           UUID REFERENCES approval(id),
    ADD COLUMN payload_hash          TEXT,
    ADD COLUMN requirements_hash     TEXT,
    ADD COLUMN architecture_doc_hash TEXT,
    -- Where the document came from (a path, or 'inline') and the SHA-256
    -- of the file's bytes. Both are in the approved payload: the human
    -- approves a named document, not a bare content hash.
    ADD COLUMN architecture_doc_ref  TEXT,
    ADD COLUMN architecture_doc_file_hash TEXT,
    -- The lock request that was the latest one when this lock's request was
    -- made (NULL for a project's first). Part of the approved payload: it
    -- ties an approval to one point in the REQUEST history, so content that
    -- was approved, then replaced by a later request, needs approving again.
    -- A plain UUID, not a foreign key: it is evidence of an order of events,
    -- and nothing should be able to block or cascade through it.
    ADD COLUMN follows_request_id    UUID,
    -- The requirements confirmation in force when the lock was requested.
    -- Part of the approved payload: confirm A, request a lock, confirm B,
    -- confirm A again, and A's content is back with a NEW confirmation. A
    -- lock approval given under the first one does not carry over.
    ADD COLUMN requirements_approval_id UUID,
    ADD COLUMN project_decisions     JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN scope                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- The lock version this one replaced (NULL for a first lock). It is part
    -- of the approved payload, which ties every approval to one point in
    -- the lock history: an approval given against v1 cannot be replayed to
    -- write a lock after v2 exists.
    ADD COLUMN replaces_version      INTEGER;

-- One lock per approval, ever.
CREATE UNIQUE INDEX architecture_lock_approval_unique
    ON architecture_lock(approval_id) WHERE approval_id IS NOT NULL;

-- The v1.0 rule guarded `decisions` only and swallowed the write
-- silently. Replace it with whole-row immutability that raises. The only
-- legal update is active -> superseded.
DROP RULE architecture_lock_no_update_decisions ON architecture_lock;

CREATE FUNCTION architecture_lock_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION
            'architecture_lock % is immutable: a change is a new version, never an edit',
            OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'active' AND NEW.status = 'superseded') THEN
        RAISE EXCEPTION
            'architecture_lock %: status may only move active -> superseded', OLD.id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER architecture_lock_immutable_trg
    BEFORE UPDATE ON architecture_lock
    FOR EACH ROW EXECUTE FUNCTION architecture_lock_immutable();

-- ---------------------------------------------------------------------
-- J. requirement_verification — structured evidence
--
-- recorded_by:
--   agent_reported — the coding agent told CIP the outcome
--   cip_executed   — `cip-composer verify` ran the command itself and
--                    observed the exit code, output and commit
--
-- build_task_id is not part of the CHECK: it is ON DELETE SET NULL, and
-- a project-level cascade must not trip over its own evidence rows. The
-- task link is enforced in the Python layer.
--
-- legacy marks rows written before this migration. They are exempt from
-- the CHECK by value rather than by NOT VALID: a NOT VALID constraint is
-- still re-checked whenever a row is updated, and deleting a v1.0 task
-- updates its verification rows (build_task_id SET NULL), which would
-- then fail. Legacy rows carry no criterion, so they never count as
-- verification anyway.
--
-- recorded_seq is insertion order. "Latest wins" uses it, not
-- verified_at: several verifications reported in one call share a
-- transaction timestamp.
--
-- exit_code is BIGINT because a Windows crash code such as 0xC0000005
-- does not fit a 32-bit integer.
-- ---------------------------------------------------------------------
ALTER TABLE requirement_verification
    ADD COLUMN acceptance_criterion_id UUID REFERENCES acceptance_criterion(id) ON DELETE CASCADE,
    ADD COLUMN commit_sha     TEXT,
    ADD COLUMN test_ref       TEXT,
    ADD COLUMN command        TEXT,
    ADD COLUMN exit_code      BIGINT,
    ADD COLUMN output_digest  TEXT,
    ADD COLUMN worktree_dirty BOOLEAN,
    ADD COLUMN recorded_by    TEXT NOT NULL DEFAULT 'agent_reported'
        CHECK (recorded_by IN ('agent_reported', 'cip_executed')),
    ADD COLUMN legacy         BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN recorded_seq   BIGINT GENERATED ALWAYS AS IDENTITY;

UPDATE requirement_verification SET legacy = true;

ALTER TABLE requirement_verification
    ADD CONSTRAINT requirement_verification_pass_evidence
        CHECK (
            legacy
            OR outcome <> 'pass'
            OR (acceptance_criterion_id IS NOT NULL
                AND commit_sha ~ '^[0-9a-fA-F]{7,64}$'
                AND length(trim(coalesce(test_ref, ''))) > 0
                AND length(trim(coalesce(command, ''))) > 0
                AND exit_code = 0)
        );

CREATE INDEX requirement_verification_criterion_idx
    ON requirement_verification(acceptance_criterion_id, recorded_seq);

-- ---------------------------------------------------------------------
-- K. evaluation — evidence depth; align the verdict set
--
-- evidence_depth says how far the evaluator actually looked. A code
-- quality grade is only allowed when the code was read; a README-only
-- evaluation can no longer be saved looking like a graded one. NULL means
-- "not recorded" and only exists on rows saved before this migration.
--
-- 'Wrap' joins the verdict set so the evaluation store, the playbook and
-- the recommendation table share the same reuse verdicts.
-- ---------------------------------------------------------------------
ALTER TABLE evaluation
    ADD COLUMN evidence_depth TEXT
        CHECK (evidence_depth IN ('code_read', 'docs_only', 'metadata_only'));

ALTER TABLE evaluation DROP CONSTRAINT evaluation_verdict_check;
ALTER TABLE evaluation
    ADD CONSTRAINT evaluation_verdict_check
        CHECK (verdict IN ('Adopt', 'Adapt', 'Wrap', 'Reference', 'Reject', 'Build')),
    ADD CONSTRAINT evaluation_grade_requires_code_read
        CHECK (evidence_depth IS NULL
               OR evidence_depth = 'code_read'
               OR code_quality IS NULL);
