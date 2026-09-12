"""Tests for the evaluation store (migration 029 + eval_queries)."""
from __future__ import annotations

from mcp_server import eval_queries


def _record(conn, **overrides):
    base = dict(
        asset_slug="acme-widget",
        verdict="Reference",
        asset_type="repo",
        url="https://github.com/acme/widget",
        code_quality="acceptable",
        banked=True,
        summary="A widget library.",
        useful_parts="src/queue.py",
        problem_types=["retry-with-backoff", "queue"],
        projects=[],
        evaluated_at="2026-09-12",
    )
    base.update(overrides)
    return eval_queries.record_evaluation(conn, **base)


class TestRecord:
    def test_insert(self, conn):
        result = _record(conn)
        assert result["asset_slug"] == "acme-widget"
        assert result["verdict"] == "Reference"
        assert result["banked"] is True
        assert result["problem_types"] == ["retry-with-backoff", "queue"]

    def test_upsert_overwrites(self, conn):
        _record(conn, verdict="Reference")
        updated = _record(conn, verdict="Adopt", summary="Changed my mind.")
        assert updated["verdict"] == "Adopt"
        assert updated["summary"] == "Changed my mind."
        # Still one row.
        rows = eval_queries.list_evaluations(conn)
        assert len([r for r in rows if r["asset_slug"] == "acme-widget"]) == 1

    def test_banked_references_roundtrip(self, conn):
        refs = [{"file": "src/queue.py", "problem": "retry", "why": "clean backoff"}]
        result = _record(conn, banked_references=refs)
        assert result["banked_references"] == refs

    def test_invalid_verdict(self, conn):
        result = _record(conn, verdict="Maybe")
        assert "error" in result

    def test_invalid_asset_type(self, conn):
        result = _record(conn, asset_type="nonsense")
        assert "error" in result

    def test_empty_slug(self, conn):
        result = _record(conn, asset_slug="  ")
        assert "error" in result


class TestGet:
    def test_get_existing(self, conn):
        _record(conn)
        got = eval_queries.get_evaluation(conn, "acme-widget")
        assert got is not None
        assert got["asset_slug"] == "acme-widget"

    def test_get_missing(self, conn):
        assert eval_queries.get_evaluation(conn, "does-not-exist") is None


class TestSearch:
    def test_by_problem_type(self, conn):
        _record(conn, asset_slug="a", problem_types=["oauth-flow"])
        _record(conn, asset_slug="b", problem_types=["retry-with-backoff"])
        rows = eval_queries.search_evaluations(conn, problem_types=["oauth-flow"])
        slugs = {r["asset_slug"] for r in rows}
        assert "a" in slugs
        assert "b" not in slugs

    def test_by_problem_type_overlap(self, conn):
        _record(conn, asset_slug="a", problem_types=["oauth-flow", "queue"])
        rows = eval_queries.search_evaluations(
            conn, problem_types=["queue", "websocket"],
        )
        assert {r["asset_slug"] for r in rows} == {"a"}

    def test_banked_filter(self, conn):
        _record(conn, asset_slug="kept", banked=True)
        _record(conn, asset_slug="junk-ish", banked=False, verdict="Reject")
        rows = eval_queries.search_evaluations(conn, banked=True)
        slugs = {r["asset_slug"] for r in rows}
        assert "kept" in slugs
        assert "junk-ish" not in slugs

    def test_by_verdict(self, conn):
        _record(conn, asset_slug="a", verdict="Adopt")
        _record(conn, asset_slug="b", verdict="Reject")
        rows = eval_queries.search_evaluations(conn, verdict="Adopt")
        assert {r["asset_slug"] for r in rows} == {"a"}

    def test_by_project(self, conn):
        _record(conn, asset_slug="a", projects=["virtual-employee"])
        _record(conn, asset_slug="b", projects=["garage-ops"])
        rows = eval_queries.search_evaluations(conn, project="virtual-employee")
        assert {r["asset_slug"] for r in rows} == {"a"}

    def test_query_substring(self, conn):
        _record(conn, asset_slug="a", summary="handles websocket reconnection")
        _record(conn, asset_slug="b", summary="a json parser")
        rows = eval_queries.search_evaluations(conn, query="websocket")
        assert {r["asset_slug"] for r in rows} == {"a"}

    def test_asset_type_filter(self, conn):
        _record(conn, asset_slug="a", asset_type="skill")
        _record(conn, asset_slug="b", asset_type="repo")
        rows = eval_queries.search_evaluations(conn, asset_type="skill")
        assert {r["asset_slug"] for r in rows} == {"a"}

    def test_empty_store(self, conn):
        assert eval_queries.search_evaluations(conn, query="anything") == []
