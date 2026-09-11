-- Migration 020 — License enforcement (Phase 1)
--
-- Makes license clearance a hard prerequisite for capability selection.
-- No component proceeds to recommendation without license_status set.
--
-- Changes:
--   1. capability.license_status — tracks license clearance outcome.
--   2. capability_version.license_source, .license_scope,
--      .copyright_notices — where the license was found and what it says.
--   3. Expanded lifecycle states on capability_version:
--      - license_checked   — license has been evaluated
--      - blocked_from_reuse — license gate failed, cannot be selected
--      - needs_review      — license is ambiguous, requires human decision
--   4. license_policy_profile — project-level license policy (which
--      licenses are acceptable for a given project).

-- ---------------------------------------------------------------------
-- capability.license_status
-- Fast-path answer: can we reuse this at all?
-- ---------------------------------------------------------------------

ALTER TABLE capability
    ADD COLUMN license_status TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE capability
    ADD CONSTRAINT capability_license_status_valid
        CHECK (license_status IN (
            'verified_open_source',
            'needs_review',
            'blocked',
            'unknown'
        ));

CREATE INDEX capability_license_status_idx
    ON capability(license_status);

-- ---------------------------------------------------------------------
-- capability_version license detail columns
-- These trace HOW the license was determined for a specific version.
-- ---------------------------------------------------------------------

ALTER TABLE capability_version
    ADD COLUMN license_source TEXT;
    -- Where the license was detected: 'license_file', 'package_metadata',
    -- 'spdx_header', 'api_metadata', 'manual_override'

ALTER TABLE capability_version
    ADD COLUMN license_scope TEXT NOT NULL DEFAULT 'repository';
    -- 'repository' (whole repo), 'directory' (subdirectory-specific),
    -- 'file' (per-file headers)

ALTER TABLE capability_version
    ADD COLUMN copyright_notices TEXT[];
    -- Extracted copyright lines, e.g. {'Copyright 2024 Acme Corp'}

-- ---------------------------------------------------------------------
-- Expanded lifecycle states
-- Adds license_checked, blocked_from_reuse, needs_review to the
-- existing CHECK constraint on capability_version.lifecycle_state.
-- We drop and recreate the constraint.
-- ---------------------------------------------------------------------

ALTER TABLE capability_version
    DROP CONSTRAINT IF EXISTS capability_version_lifecycle_valid;

ALTER TABLE capability_version
    ADD CONSTRAINT capability_version_lifecycle_valid
        CHECK (lifecycle_state IN (
            'candidate',
            'license_checked',
            'analyzed',
            'verified',
            'cataloged',
            'stale',
            'quarantined',
            'deprecated',
            'revoked',
            'blocked_from_reuse',
            'needs_review'
        ));

-- Index for quick "what's blocked" queries.
CREATE INDEX capability_version_blocked_idx
    ON capability_version(capability_id)
    WHERE lifecycle_state = 'blocked_from_reuse';

CREATE INDEX capability_version_needs_review_idx
    ON capability_version(capability_id)
    WHERE lifecycle_state = 'needs_review';

-- ---------------------------------------------------------------------
-- license_policy_profile
-- Project-level license policy. A project can override the global
-- ACCEPTABLE_LICENSES set with its own allowlist/denylist.
-- ---------------------------------------------------------------------

CREATE TABLE license_policy_profile (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID        REFERENCES project(id) ON DELETE CASCADE,
    name           TEXT        NOT NULL,
    allowed_spdx   TEXT[]      NOT NULL DEFAULT '{}',
    denied_spdx    TEXT[]      NOT NULL DEFAULT '{}',
    require_notice BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT license_policy_unique_per_project
        UNIQUE (project_id, name)
);

-- A NULL project_id row is the global default policy.
CREATE UNIQUE INDEX license_policy_global_default_idx
    ON license_policy_profile(name)
    WHERE project_id IS NULL;
