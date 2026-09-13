-- Migration 019 — Search quality (Phase 0.5)
--
-- Three improvements:
--   1. pg_trgm extension + GIN trigram indexes on display_name and
--      normalized_key for fuzzy/similarity search.
--   2. GIN index on metadata->'topics' for tag-based discovery.
--   3. GIN index on metadata->'description' and metadata->'gemini_description'
--      for description full-text search via tsvector.
--
-- The trigram indexes let search_capabilities use similarity() and the
-- % operator instead of ILIKE '%term%', which:
--   - Handles typos and partial matches ("reqests" finds "requests")
--   - Returns a relevance score (0..1) for ranking
--   - Uses the GIN index for performance on large registries
--
-- The topics GIN index lets queries filter by tag efficiently using
-- the @> (contains) operator on the JSONB array.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Trigram indexes for fuzzy search on the two primary search fields.
CREATE INDEX capability_display_name_trgm_idx
    ON capability USING GIN (lower(display_name) gin_trgm_ops);

CREATE INDEX capability_normalized_key_trgm_idx
    ON capability USING GIN (lower(normalized_key) gin_trgm_ops);

-- GIN index on metadata->'topics' for tag filtering.
-- Topics are stored as a JSONB array: ["video", "ai", "python"].
CREATE INDEX capability_metadata_topics_idx
    ON capability USING GIN ((metadata -> 'topics'));

-- Full-text search index on description fields in metadata.
-- Combines description and gemini_description into one tsvector.
CREATE INDEX capability_description_fts_idx
    ON capability USING GIN (
        to_tsvector('english',
            COALESCE(metadata->>'description', '') || ' ' ||
            COALESCE(metadata->>'gemini_description', '')
        )
    );
