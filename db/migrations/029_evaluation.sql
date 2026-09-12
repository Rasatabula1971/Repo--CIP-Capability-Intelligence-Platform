-- Migration 029 — evaluation store
--
-- Durable, searchable home for repo-scout / co-work asset evaluations.
-- Standalone by design: evaluations are cross-project (an asset can be
-- banked for the future with no current project), so no project FK.
-- Replaces the lossy markdown-in-memory approach — one row per asset,
-- upserted by asset_slug, recalled by problem_types.

CREATE TABLE evaluation (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_slug         TEXT        NOT NULL UNIQUE,
    asset_type         TEXT        NOT NULL DEFAULT 'repo'
                         CHECK (asset_type IN ('repo', 'skill', 'agent',
                                               'mcp_server', 'prompt', 'workflow')),
    url                TEXT,
    verdict            TEXT        NOT NULL
                         CHECK (verdict IN ('Adopt', 'Adapt', 'Reference',
                                            'Reject', 'Build')),
    code_quality       TEXT        CHECK (code_quality IN ('solid', 'acceptable',
                                                           'fragile', 'untested')),
    banked             BOOLEAN     NOT NULL DEFAULT false,
    summary            TEXT        NOT NULL DEFAULT '',
    code_quality_notes TEXT        NOT NULL DEFAULT '',
    useful_parts       TEXT        NOT NULL DEFAULT '',
    skip_notes         TEXT        NOT NULL DEFAULT '',
    banked_references  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    projects           TEXT[]      NOT NULL DEFAULT '{}',
    problem_types      TEXT[]      NOT NULL DEFAULT '{}',
    license_notes      TEXT        NOT NULL DEFAULT '',
    cip_status         TEXT        NOT NULL DEFAULT 'not-ingested',
    evaluated_at       DATE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX evaluation_verdict_idx       ON evaluation(verdict);
CREATE INDEX evaluation_banked_idx        ON evaluation(banked);
CREATE INDEX evaluation_asset_type_idx    ON evaluation(asset_type);
CREATE INDEX evaluation_problem_types_idx ON evaluation USING GIN (problem_types);
CREATE INDEX evaluation_projects_idx      ON evaluation USING GIN (projects);
