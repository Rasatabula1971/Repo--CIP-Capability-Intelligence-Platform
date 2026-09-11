-- Migration 021 — Dependency Intelligence (Phase 2)
--
-- Two new tables:
--   dependency_fact       — broader dependency facts beyond package deps:
--                           runtime versions, OS, services, system libs,
--                           environment variables, databases, API keys.
--   environment_profile   — describes a target environment that a project
--                           will run in: OS, runtime versions, available
--                           services, hardware.
--
-- Together with the existing capability_dependency table (package deps),
-- these let dependency_fit() answer "will this capability actually work
-- in my environment?" before selection.

-- ---------------------------------------------------------------------
-- dependency_fact
-- A single environmental dependency declared by or inferred from a
-- capability version. Broader than capability_dependency (which tracks
-- package-level deps); this covers runtime version requirements, OS
-- constraints, service dependencies, system libraries, etc.
--
-- fact_kind determines interpretation:
--   runtime_version   — needs Python>=3.9, Node>=18, etc.
--   os                — needs linux, darwin, win32
--   system_library    — needs libpq, libssl, ffmpeg binary
--   service           — needs PostgreSQL, Redis, S3-compatible store
--   environment_var   — needs DATABASE_URL, API_KEY set
--   hardware          — needs GPU, >=8GB RAM, x86_64
-- ---------------------------------------------------------------------
CREATE TABLE dependency_fact (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    evidence_item_id      UUID        REFERENCES evidence_item(id),
    fact_kind             TEXT        NOT NULL,
    fact_key              TEXT        NOT NULL,
    version_spec          TEXT        NOT NULL DEFAULT '',
    required              BOOLEAN     NOT NULL DEFAULT TRUE,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dependency_fact_kind_valid
        CHECK (fact_kind IN (
            'runtime_version',
            'os',
            'system_library',
            'service',
            'environment_var',
            'hardware'
        ))
);

CREATE INDEX dependency_fact_version_idx
    ON dependency_fact(capability_version_id);
CREATE INDEX dependency_fact_kind_idx
    ON dependency_fact(fact_kind, fact_key);

-- ---------------------------------------------------------------------
-- environment_profile
-- Describes a target environment for dependency fit checking. Linked
-- to a project — a project declares "I run on Linux with Python 3.11,
-- PostgreSQL available, no GPU."
--
-- The profile is a JSONB document with a known schema:
-- {
--   "os": ["linux"],
--   "arch": ["x86_64"],
--   "runtimes": {"python": "3.11.4", "node": "20.9.0"},
--   "services": ["postgresql", "redis"],
--   "system_libraries": ["libpq", "libssl"],
--   "environment_vars": ["DATABASE_URL", "REDIS_URL"],
--   "hardware": {"gpu": false, "min_ram_gb": 4}
-- }
-- ---------------------------------------------------------------------
CREATE TABLE environment_profile (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL DEFAULT 'default',
    profile     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT environment_profile_unique
        UNIQUE (project_id, name)
);
