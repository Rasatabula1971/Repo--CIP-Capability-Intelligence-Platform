"""
Step 14 (path B) done-when for slice 1:
  "An LLM registering the CIP MCP server can search the capability
   registry by keyword and fetch a full record for one capability,
   including interfaces, dependencies, and (if scored) scorecard
   dimensions."

Test coverage:
  1. search_capabilities:
     - Empty query returns []
     - Substring match on display_name and normalized_key
     - Case-insensitivity
     - Filter by ecosystem and kind
     - Ordering by intrinsic score (scored rows first, unscored last)
     - Limit clamped to [1, 100]
     - Rows that have no head version still appear (superseded-only capability)
  2. capability_detail:
     - Unknown UUID returns None
     - Malformed id returns None (not an exception)
     - Returns metadata, head version, interfaces, dependencies
     - Interfaces + deps carry evidence_item_id
     - Scorecard is None when no scorecard exists; populated with
       dimensions when it does
"""
from __future__ import annotations

import uuid

import pytest

from mcp_server import queries


# ---------------------------------------------------------------------------
# Low-level seed helpers — direct inserts. We want to test the queries in
# isolation, not the whole promotion pipeline.
# ---------------------------------------------------------------------------

def _mk_provider_asset_revision(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, 'repository') RETURNING id",
            (provider_id, f"a/{uuid.uuid4().hex[:6]}", "repo"),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, uuid.uuid4().hex),
        )
        rev_id = cur.fetchone()[0]
    return rev_id


def _mk_evidence(conn, rev_id: uuid.UUID) -> uuid.UUID:
    """One minimal evidence_item so interfaces/dependencies can FK to it."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run "
            "(source_revision_id, extractor_config_version, status) "
            "VALUES (%s, 1, 'completed') RETURNING id",
            (rev_id,),
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO evidence_item "
            "(source_revision_id, analysis_run_id, extractor_name, "
            " evidence_type, locator_kind, locator, extracted_value) "
            "VALUES (%s, %s, 'test', 'interface', 'whole_file', "
            " '{\"path\": \"src/x.py\"}'::jsonb, '{}'::jsonb) "
            "RETURNING id",
            (rev_id, run_id),
        )
        return cur.fetchone()[0]


def _mk_capability(
    conn,
    normalized_key: str,
    display_name: str,
    ecosystem: str | None = "pypi",
    kind: str = "library",
    metadata: dict | None = None,
) -> uuid.UUID:
    import json as _json
    meta = _json.dumps(metadata or {})
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability (normalized_key, display_name, ecosystem, kind, metadata) "
            "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
            (normalized_key, display_name, ecosystem, kind, meta),
        )
        return cur.fetchone()[0]


def _mk_version(
    conn,
    capability_id: uuid.UUID,
    version_key: str = "content:abc",
    superseded_by_id: uuid.UUID | None = None,
    display_version: str | None = "0.1.0",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version, superseded_by_id) "
            "VALUES (%s, %s, 'content-hash', %s, %s) RETURNING id",
            (capability_id, version_key, display_version, superseded_by_id),
        )
        return cur.fetchone()[0]


def _mk_scoring_profile(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scoring_profile "
            "(name, version, profile_hash, dimensions) "
            "VALUES (%s, 1, %s, '{}'::jsonb) RETURNING id",
            (f"prof-{uuid.uuid4().hex[:6]}", uuid.uuid4().hex),
        )
        return cur.fetchone()[0]


def _mk_scorecard(
    conn,
    version_id: uuid.UUID,
    profile_id: uuid.UUID,
    total: float = 0.72,
    confidence: float = 0.90,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scorecard "
            "(capability_version_id, scoring_profile_id, total_score, "
            " confidence, computed_hash) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (version_id, profile_id, total, confidence, uuid.uuid4().hex),
        )
        card_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO score_dimension_result "
            "(scorecard_id, dimension_name, raw_score, weight, "
            " weighted_score, coverage, contradiction, evidence_count) "
            "VALUES (%s, 'health', 0.800, 0.500, 0.4000, 1.000, 0.000, 3)",
            (card_id,),
        )
    return card_id


# ---------------------------------------------------------------------------
# search_capabilities
# ---------------------------------------------------------------------------

def test_search_empty_query_returns_empty_list(conn):
    assert queries.search_capabilities(conn, query="") == []
    assert queries.search_capabilities(conn, query="   ") == []


def test_search_matches_display_name_substring(conn):
    _mk_capability(conn, "pypi:requests", "requests")
    _mk_capability(conn, "pypi:httpx", "httpx")
    conn.commit()

    rows = queries.search_capabilities(conn, query="req")
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:requests" in keys
    assert "pypi:httpx" not in keys


def test_search_matches_normalized_key_substring(conn):
    _mk_capability(conn, "npm:react-router", "React Router")
    conn.commit()

    rows = queries.search_capabilities(conn, query="react-rou")
    assert len(rows) == 1
    assert rows[0]["display_name"] == "React Router"


def test_search_is_case_insensitive(conn):
    _mk_capability(conn, "pypi:requests", "Requests")
    conn.commit()

    assert len(queries.search_capabilities(conn, query="REQUESTS")) == 1
    assert len(queries.search_capabilities(conn, query="reQUests")) == 1


def test_search_filter_by_ecosystem(conn):
    _mk_capability(conn, "pypi:foo", "foo", ecosystem="pypi")
    _mk_capability(conn, "npm:foo", "foo", ecosystem="npm")
    conn.commit()

    rows = queries.search_capabilities(conn, query="foo", ecosystem="npm")
    assert [r["normalized_key"] for r in rows] == ["npm:foo"]


def test_search_filter_by_capability_kind(conn):
    _mk_capability(conn, "pypi:libfoo", "libfoo", kind="library")
    _mk_capability(conn, "pypi:cli-foo", "cli-foo", kind="cli")
    conn.commit()

    rows = queries.search_capabilities(conn, query="foo", capability_kind="cli")
    assert [r["normalized_key"] for r in rows] == ["pypi:cli-foo"]


def test_search_orders_scored_before_unscored_and_by_score(conn):
    cap_a = _mk_capability(conn, "pypi:aaa", "aaa")
    cap_b = _mk_capability(conn, "pypi:bbb", "bbb")
    cap_c = _mk_capability(conn, "pypi:ccc", "ccc")
    ver_a = _mk_version(conn, cap_a)
    ver_b = _mk_version(conn, cap_b)
    _mk_version(conn, cap_c)  # no scorecard
    profile = _mk_scoring_profile(conn)
    _mk_scorecard(conn, ver_a, profile, total=0.40)
    _mk_scorecard(conn, ver_b, profile, total=0.90)
    conn.commit()

    rows = queries.search_capabilities(conn, query="")  # empty → nothing
    assert rows == []

    rows = queries.search_capabilities(conn, query="p")  # 'p' is in every key
    order = [r["normalized_key"] for r in rows]
    assert order.index("pypi:bbb") < order.index("pypi:aaa") < order.index("pypi:ccc")


def test_search_limit_is_clamped(conn):
    for i in range(3):
        _mk_capability(conn, f"pypi:pkg{i}", f"pkg{i}")
    conn.commit()

    # limit <1 clamps to 1
    assert len(queries.search_capabilities(conn, query="pkg", limit=0)) == 1
    assert len(queries.search_capabilities(conn, query="pkg", limit=-5)) == 1
    # limit >100 clamps to 100 (only 3 rows exist so we just verify no crash)
    assert len(queries.search_capabilities(conn, query="pkg", limit=999)) == 3


def test_search_returns_capability_with_no_head_version(conn):
    """A capability whose only version is superseded still shows up —
    the head_version_id will be null."""
    cap = _mk_capability(conn, "pypi:legacy", "legacy")
    v1 = _mk_version(conn, cap, version_key="content:v1")
    v2 = _mk_version(conn, cap, version_key="content:v2")
    # Mark v1 superseded by v2, then supersede v2 by v1 to create a fully-
    # superseded lineage. In practice this is a degenerate but legal state.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability_version SET superseded_by_id = %s WHERE id = %s",
            (v2, v1),
        )
        cur.execute(
            "UPDATE capability_version SET superseded_by_id = %s WHERE id = %s",
            (v1, v2),
        )
    conn.commit()

    rows = queries.search_capabilities(conn, query="legacy")
    assert len(rows) == 1
    assert rows[0]["head_version_id"] is None


# ---------------------------------------------------------------------------
# capability_detail
# ---------------------------------------------------------------------------

def test_detail_unknown_uuid_returns_none(conn):
    assert queries.capability_detail(conn, str(uuid.uuid4())) is None


def test_detail_malformed_id_returns_none(conn):
    assert queries.capability_detail(conn, "not-a-uuid") is None
    assert queries.capability_detail(conn, "") is None


def test_detail_returns_metadata_and_head_version(conn):
    cap = _mk_capability(conn, "pypi:requests", "requests")
    ver = _mk_version(conn, cap, display_version="2.31.0")
    conn.commit()

    detail = queries.capability_detail(conn, str(cap))
    assert detail is not None
    assert detail["normalized_key"] == "pypi:requests"
    assert detail["display_name"] == "requests"
    assert detail["head_version"]["display_version"] == "2.31.0"
    assert detail["interfaces"] == []
    assert detail["dependencies"] == []
    assert detail["scorecard"] is None


def test_detail_returns_interfaces_and_dependencies_with_evidence(conn):
    cap = _mk_capability(conn, "pypi:requests", "requests")
    ver = _mk_version(conn, cap)
    rev = _mk_provider_asset_revision(conn)
    ev_iface = _mk_evidence(conn, rev)
    ev_dep = _mk_evidence(conn, rev)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_interface "
            "(capability_version_id, evidence_item_id, kind, name, signature, language) "
            "VALUES (%s, %s, 'function', 'get', 'get(url, **kwargs)', 'python')",
            (ver, ev_iface),
        )
        cur.execute(
            "INSERT INTO capability_dependency "
            "(capability_version_id, evidence_item_id, depends_on_ecosystem, "
            " depends_on_name, version_spec, dep_kind) "
            "VALUES (%s, %s, 'pypi', 'urllib3', '>=2.0', 'runtime')",
            (ver, ev_dep),
        )
    conn.commit()

    detail = queries.capability_detail(conn, str(cap))
    assert len(detail["interfaces"]) == 1
    iface = detail["interfaces"][0]
    assert iface["name"] == "get"
    assert iface["evidence_item_id"] == str(ev_iface)
    assert len(detail["dependencies"]) == 1
    dep = detail["dependencies"][0]
    assert dep["name"] == "urllib3"
    assert dep["evidence_item_id"] == str(ev_dep)


def test_detail_includes_scorecard_with_dimensions_when_present(conn):
    cap = _mk_capability(conn, "pypi:requests", "requests")
    ver = _mk_version(conn, cap)
    profile = _mk_scoring_profile(conn)
    _mk_scorecard(conn, ver, profile, total=0.72, confidence=0.9)
    conn.commit()

    detail = queries.capability_detail(conn, str(cap))
    card = detail["scorecard"]
    assert card is not None
    assert card["total_score"] == pytest.approx(0.72)
    assert card["confidence"] == pytest.approx(0.9)
    assert len(card["dimensions"]) == 1
    dim = card["dimensions"][0]
    assert dim["name"] == "health"
    assert dim["raw_score"] == pytest.approx(0.8)
    assert dim["evidence_count"] == 3


# ---------------------------------------------------------------------------
# Phase 0.5 — search quality upgrade tests
# ---------------------------------------------------------------------------

def test_search_returns_text_relevance_field(conn):
    """Upgraded search includes a text_relevance score in results."""
    _mk_capability(conn, "pypi:requests", "requests")
    conn.commit()

    rows = queries.search_capabilities(conn, query="requests")
    assert len(rows) == 1
    assert "text_relevance" in rows[0]
    assert isinstance(rows[0]["text_relevance"], (int, float))
    assert rows[0]["text_relevance"] > 0


def test_search_exact_match_ranks_higher_than_substring(conn):
    """An exact name match should score higher than a partial match."""
    _mk_capability(conn, "pypi:click", "click")
    _mk_capability(conn, "pypi:clicker-tool", "clicker-tool")
    conn.commit()

    rows = queries.search_capabilities(conn, query="click")
    assert len(rows) >= 2
    rel_exact = next(r["text_relevance"] for r in rows if r["normalized_key"] == "pypi:click")
    rel_sub = next(r["text_relevance"] for r in rows if r["normalized_key"] == "pypi:clicker-tool")
    assert rel_exact >= rel_sub


def test_search_filter_by_single_tag(conn):
    """The tags parameter filters to rows whose metadata.topics contain the tag."""
    _mk_capability(conn, "pypi:opencv", "OpenCV",
                   metadata={"topics": ["video", "ai", "python"]})
    _mk_capability(conn, "pypi:pillow", "Pillow",
                   metadata={"topics": ["image", "python"]})
    conn.commit()

    rows = queries.search_capabilities(conn, query="p", tags=["video"])
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:opencv" in keys
    assert "pypi:pillow" not in keys


def test_search_filter_by_multiple_tags_requires_all(conn):
    """Multiple tags use AND — the row must contain every tag."""
    _mk_capability(conn, "pypi:torch", "PyTorch",
                   metadata={"topics": ["ai", "gpu", "python"]})
    _mk_capability(conn, "pypi:numpy", "NumPy",
                   metadata={"topics": ["math", "python"]})
    _mk_capability(conn, "pypi:jax", "JAX",
                   metadata={"topics": ["ai", "python"]})
    conn.commit()

    rows = queries.search_capabilities(conn, query="p", tags=["ai", "gpu"])
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:torch" in keys
    assert "pypi:numpy" not in keys
    assert "pypi:jax" not in keys


def test_search_tags_empty_list_returns_all(conn):
    """An empty tags list has no filtering effect."""
    _mk_capability(conn, "pypi:foo", "foo",
                   metadata={"topics": ["web"]})
    _mk_capability(conn, "pypi:bar", "bar",
                   metadata={"topics": ["cli"]})
    conn.commit()

    rows_no_tags = queries.search_capabilities(conn, query="")
    rows_empty = queries.search_capabilities(conn, query="foo", tags=[])
    # Empty tags list should behave like no tags param
    assert len(rows_empty) == len(
        queries.search_capabilities(conn, query="foo", tags=None)
    )


def test_search_tags_with_no_topics_in_metadata(conn):
    """Rows with no topics key in metadata are excluded by tag filter."""
    _mk_capability(conn, "pypi:bare", "bare")  # no metadata.topics
    _mk_capability(conn, "pypi:tagged", "tagged",
                   metadata={"topics": ["web"]})
    conn.commit()

    rows = queries.search_capabilities(conn, query="b", tags=["web"])
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:bare" not in keys


def test_search_composite_ranking_weighs_score(conn):
    """Higher intrinsic score should boost ranking when text relevance is similar."""
    cap_a = _mk_capability(conn, "pypi:flask-a", "flask-a")
    cap_b = _mk_capability(conn, "pypi:flask-b", "flask-b")
    ver_a = _mk_version(conn, cap_a)
    ver_b = _mk_version(conn, cap_b)
    profile = _mk_scoring_profile(conn)
    _mk_scorecard(conn, ver_a, profile, total=0.20)
    _mk_scorecard(conn, ver_b, profile, total=0.95)
    conn.commit()

    rows = queries.search_capabilities(conn, query="flask")
    keys = [r["normalized_key"] for r in rows]
    assert keys.index("pypi:flask-b") < keys.index("pypi:flask-a")


def test_search_combines_tags_with_other_filters(conn):
    """Tags work alongside ecosystem and kind filters."""
    _mk_capability(conn, "pypi:web-py", "web-py", ecosystem="pypi", kind="library",
                   metadata={"topics": ["web"]})
    _mk_capability(conn, "npm:web-js", "web-js", ecosystem="npm", kind="library",
                   metadata={"topics": ["web"]})
    conn.commit()

    rows = queries.search_capabilities(
        conn, query="web", ecosystem="pypi", tags=["web"],
    )
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:web-py" in keys
    assert "npm:web-js" not in keys


def test_has_pg_trgm_helper(conn):
    """_has_pg_trgm returns a bool and doesn't crash."""
    result = queries._has_pg_trgm(conn)
    assert isinstance(result, bool)
