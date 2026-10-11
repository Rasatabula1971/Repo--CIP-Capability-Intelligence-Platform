"""
PDR-to-Build workflow — integration tests against real Postgres.

Implementation Spec v1.1. Each class covers one stage of the workflow and
the gap(s) it closes; the gap numbers refer to the v1.0 review recorded in
docs/pdr_to_build_spec_v1_1.md.

The pure rules are unit-tested in test_pdr_core.py. These tests check that
the tools enforce them, that refusals leave no partial state, and that the
database refuses what the Python layer refuses.
"""
from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from core.policy.build_gate import can_generate_for
from mcp_server import pdr_approvals, queries
from mcp_server import pdr_queries as q
from mcp_server.eval_queries import record_evaluation
from tests.pdr_helpers import (
    LICENSE_CONSTRAINT,
    NEED,
    PASS,
    PDR,
    REQS,
    approve,
    bespoke_task,
    confirm,
    decided_adopt,
    decided_build,
    ingest,
    lock,
    locked_adopt,
    locked_build,
    make_capability,
    record_need,
    record_reqs,
    reject,
    scalar,
    scoped,
    with_requirements,
)


# ---------------------------------------------------------------------------
# 1. ingest_pdr — the text is stored; revisions chain            (gaps 6, 11)
# ---------------------------------------------------------------------------

class TestIngestPdr:

    def test_stores_text_not_only_hash(self, conn):
        r = ingest(conn)
        assert r["was_duplicate"] is False and r["is_current"] is True
        stored = scalar(conn, "SELECT raw_text FROM requirement_source WHERE id = %s",
                        (r["requirement_source_id"],))
        assert stored == PDR

    def test_duplicate_returns_same_source(self, conn):
        r1, r2 = ingest(conn), ingest(conn)
        assert r2["was_duplicate"] is True
        assert r2["requirement_source_id"] == r1["requirement_source_id"]

    def test_crlf_and_bom_do_not_change_the_hash(self, conn):
        r1 = ingest(conn)
        r2 = ingest(conn, pdr="﻿" + PDR.replace("\n", "\r\n"))
        assert r2["content_hash"] == r1["content_hash"]
        assert r2["was_duplicate"] is True

    def test_reads_from_path(self, conn, tmp_path):
        f = tmp_path / "pdr.md"
        f.write_bytes(PDR.replace("\n", "\r\n").encode("utf-8"))
        r = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(f))
        assert r["content_hash"] == ingest(conn, name="other")["content_hash"]

    def test_needs_exactly_one_of_path_or_text(self, conn):
        assert "error" in q.ingest_pdr(conn, project_name="p", pdr_title="t")
        assert "error" in q.ingest_pdr(
            conn, project_name="p", pdr_title="t", pdr_text="x", pdr_path="/nope",
        )

    def test_missing_file_and_empty_text_are_errors(self, conn):
        assert "error" in q.ingest_pdr(
            conn, project_name="p", pdr_title="t", pdr_path="/no/such/file.md",
        )
        assert "error" in q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_text="  \n")

    def test_bad_project_id(self, conn):
        r = q.ingest_pdr(conn, project_name="x", pdr_title="x", pdr_text="x",
                         project_id=str(uuid.uuid4()))
        assert "error" in r

    def test_v1_0_source_without_text_is_backfilled(self, conn):
        r = ingest(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE requirement_source SET raw_text = NULL WHERE id = %s",
                        (r["requirement_source_id"],))
        conn.commit()
        again = ingest(conn)
        assert again["was_duplicate"] is True
        assert scalar(conn, "SELECT raw_text FROM requirement_source WHERE id = %s",
                      (r["requirement_source_id"],)) == PDR

    def test_revised_pdr_supersedes_and_reanchors(self, conn):
        ctx = with_requirements(conn)
        revised = PDR.replace("3. The app could have dark mode.\n", "").replace(
            "## Goals", "## Goals and background",
        )
        r = ingest(conn, pdr=revised)
        assert r["supersedes_id"] == ctx.sid
        assert r["orphaned"] == ["dark-mode"]
        assert "staff-sign-in" in r["carried_forward"]
        # The carried-forward requirement now points at the new text, with a
        # span that still selects its quote there.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT requirement_source_id, source_span FROM project_requirement "
                "WHERE id = %s", (ctx.reqs["staff-sign-in"],),
            )
            src, span = cur.fetchone()
        assert str(src) == r["requirement_source_id"]
        assert revised[span["start"]:span["end"]] == REQS[0]["quote"]

    def test_recording_against_a_superseded_source_is_refused(self, conn):
        ctx = with_requirements(conn)
        ingest(conn, pdr=PDR + "\n## New\nSomething new is required here.\n")
        r = record_reqs(conn, ctx.pid, ctx.sid, [REQS[0]])
        assert "not the current PDR" in r["error"]


# ---------------------------------------------------------------------------
# 2. record_requirements — quotes are located by CIP             (gaps 1, 6)
# ---------------------------------------------------------------------------

class TestRecordRequirements:

    def test_span_is_computed_from_the_quote(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"])
        assert all(r["valid"] for r in rr["requirements"])
        span = rr["requirements"][0]["span"]
        assert PDR[span["start"]:span["end"]] == REQS[0]["quote"]

    def test_offsets_from_the_caller_are_ignored(self, conn):
        ing = ingest(conn)
        wrong = {**REQS[0], "quote": None,
                 "source_span": {"start": 3, "end": 9, "quote": REQS[0]["quote"]}}
        wrong.pop("quote")
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [wrong])
        span = rr["requirements"][0]["span"]
        assert (span["start"], span["end"]) != (3, 9)
        assert PDR[span["start"]:span["end"]] == REQS[0]["quote"]

    def test_quote_not_in_pdr_is_refused_and_writes_nothing(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [
            {**REQS[0], "quote": "Staff must sign in with Microsoft before anything else."},
        ])
        assert rr["requirements"][0]["valid"] is False
        assert "quote not found" in rr["requirements"][0]["errors"][0]
        assert scalar(conn, "SELECT count(*) FROM project_requirement") == 0

    def test_reflowed_whitespace_still_matches(self, conn):
        ing = ingest(conn, pdr=PDR.replace("Google OAuth before", "Google OAuth\n   before"))
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [REQS[0]])
        assert rr["requirements"][0]["valid"] is True

    def test_missing_acceptance_criteria(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [
            {**REQS[0], "acceptance_criteria": []},
        ])
        assert rr["requirements"][0]["valid"] is False
        assert any("acceptance_criteria" in e for e in rr["requirements"][0]["errors"])

    def test_invalid_priority(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [
            {**REQS[0], "priority": "critical"},
        ])
        assert any("priority" in e for e in rr["requirements"][0]["errors"])

    def test_disposition_needs_a_reason_and_via_needs_cannot_be_asserted(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [
            {**REQS[0], "disposition": "bespoke"},
            {**REQS[0], "disposition": "via_needs"},
        ])
        assert any("disposition_reason" in e for e in rr["requirements"][0]["errors"])
        assert any("cannot be set directly" in e for e in rr["requirements"][1]["errors"])

    def test_constraints_and_need_kind_belong_elsewhere(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [
            {**REQS[0], "constraints": [LICENSE_CONSTRAINT]},
            {**REQS[0], "kind": "capability_need"},
        ])
        assert not rr["requirements"][0]["valid"] and not rr["requirements"][1]["valid"]

    def test_criteria_become_rows_and_survive_an_edit(self, conn):
        ctx = with_requirements(conn)
        rid = ctx.reqs["staff-sign-in"]
        before = scalar(
            conn, "SELECT id FROM acceptance_criterion WHERE project_requirement_id = %s "
                  "AND ordinal = 1", (rid,),
        )
        # Re-record: keep criterion 1, replace criterion 2, add a third first.
        record_reqs(conn, ctx.pid, ctx.sid, [{
            **REQS[0],
            "acceptance_criteria": [
                "A new criterion placed first",
                REQS[0]["acceptance_criteria"][0],
                "A replacement for the second",
            ],
        }])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ordinal, text FROM acceptance_criterion "
                "WHERE project_requirement_id = %s ORDER BY ordinal", (rid,),
            )
            rows = cur.fetchall()
        assert [r[1] for r in rows] == [1, 2, 3]
        # The unchanged criterion kept its row (and so its verification history).
        assert rows[1][0] == before and rows[1][2] == REQS[0]["acceptance_criteria"][0]

    def test_upsert_keeps_the_id(self, conn):
        ctx = with_requirements(conn)
        rr = record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[0], "text": "Reworded"}])
        assert rr["requirements"][0]["req_id"] == ctx.reqs["staff-sign-in"]

    def test_reports_pdr_sections_with_no_requirement(self, conn):
        ing = ingest(conn)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"])
        cov = rr["span_coverage"]
        assert [s["heading"] for s in cov["unmapped_sections"]] == ["Goals", "Glossary"]
        # "Constraints" is quoted only by a not_applicable item. That is
        # reported on its own: it is neither unread nor in the build.
        assert [s["heading"] for s in cov["out_of_scope_only_sections"]] == ["Constraints"]
        assert cov["section_count"] == 4 and cov["mapped_section_count"] == 1

    def test_bad_ids(self, conn):
        r = q.record_requirements(conn, project_id=str(uuid.uuid4()),
                                  requirement_source_id=str(uuid.uuid4()), requirements=[])
        assert "error" in r


# ---------------------------------------------------------------------------
# 3. record_capability_needs — the unit that is searched           (gap 1)
# ---------------------------------------------------------------------------

class TestCapabilityNeeds:

    def test_links_need_and_sets_via_needs(self, conn):
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid)
        assert need["satisfies"] == ["staff-sign-in"]
        assert scalar(conn, "SELECT disposition FROM project_requirement WHERE id = %s",
                      (ctx.reqs["staff-sign-in"],)) == "via_needs"
        assert scalar(conn, "SELECT kind FROM project_requirement WHERE id = %s",
                      (need["need_id"],)) == "capability_need"

    def test_warns_when_a_need_has_no_checkable_constraint(self, conn):
        ctx = with_requirements(conn)
        assert record_need(conn, ctx.pid)["warnings"]
        assert record_need(conn, ctx.pid, constraints=[LICENSE_CONSTRAINT])["warnings"] == []

    def test_unknown_requirement_and_bad_constraint_are_refused(self, conn):
        ctx = with_requirements(conn)
        res = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "satisfies": ["no-such-req"]},
            {**NEED, "slug": "n2", "constraints": [{"kind": "bogus"}]},
            {**NEED, "slug": "n3", "search_terms": []},
        ])
        assert [n["valid"] for n in res["needs"]] == [False, False, False]
        assert scalar(conn, "SELECT count(*) FROM project_requirement "
                            "WHERE kind = 'capability_need'") == 0

    def test_need_slug_cannot_collide_with_a_requirement(self, conn):
        ctx = with_requirements(conn)
        res = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "slug": "todays-bookings"},
        ])
        assert res["needs"][0]["valid"] is False

    def test_cannot_mark_bespoke_while_a_need_is_linked(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        rr = record_reqs(conn, ctx.pid, ctx.sid, [
            {**REQS[0], "disposition": "bespoke", "disposition_reason": "changed my mind"},
        ])
        assert rr["requirements"][0]["valid"] is False
        assert "capability needs are linked" in rr["requirements"][0]["errors"][0]

    def test_relinking_replaces_links_and_reports_the_orphan(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        res = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "satisfies": ["todays-bookings"]},
        ])
        assert res["needs"][0]["satisfies"] == ["todays-bookings"]
        # staff-sign-in is still via_needs but nothing satisfies it now.
        assert any("staff-sign-in" in b and "no active capability need" in b
                   for b in res["blockers"])

    def test_database_refuses_a_malformed_link(self, conn):
        ctx = with_requirements(conn)
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO requirement_need_link (requirement_id, need_id) "
                    "VALUES (%s, %s)",
                    (ctx.reqs["staff-sign-in"], ctx.reqs["todays-bookings"]),
                )
        conn.rollback()

    def test_database_refuses_turning_a_requirement_into_a_need(self, conn):
        ctx = with_requirements(conn)
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE project_requirement SET kind = 'capability_need', "
                    "disposition = NULL WHERE id = %s", (ctx.reqs["dark-mode"],),
                )
        conn.rollback()

    def test_retire(self, conn):
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid)
        r = q.retire_requirement(conn, req_id=need["need_id"], reason="not needed after all")
        assert r["status"] == "retired"
        assert any("staff-sign-in" in b for b in r["blockers"])
        assert "error" in q.retire_requirement(conn, req_id=need["need_id"], reason=" ")
        assert q.retire_requirement(conn, req_id=str(uuid.uuid4()), reason="x") is None


# ---------------------------------------------------------------------------
# 4. Requirements confirmation — a gate with state and a human     (gap 4)
# ---------------------------------------------------------------------------

class TestRequirementsGate:

    def test_blocked_until_every_requirement_is_dispositioned(self, conn):
        ctx = with_requirements(conn)
        r = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert r["status"] == "blocked"
        assert any("staff-sign-in" in b and "no disposition" in b for b in r["blockers"])
        assert scalar(conn, "SELECT count(*) FROM approval_request") == 0

    def test_opens_one_request_for_the_same_content(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        r1 = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        r2 = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert r1["status"] == r2["status"] == "awaiting_approval"
        assert r1["approval_request_id"] == r2["approval_request_id"]
        assert "Goals" in r1["summary_markdown"]            # unmapped section surfaced

    def test_search_is_refused_until_a_human_approves(self, conn):
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid)
        r = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        refused = q.search_for_requirement(conn, req_id=need["need_id"])
        assert refused["refused"] and refused["gate"] == "requirements_confirmation"
        assert scalar(conn, "SELECT count(*) FROM requirement_search") == 0

        approve(conn, r["approval_request_id"])
        assert "candidates" in q.search_for_requirement(conn, req_id=need["need_id"])

    def test_no_mcp_path_records_an_approval(self, conn):
        """The workflow tools can ask; none of them can answer."""
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        q.request_requirements_confirmation(conn, project_id=ctx.pid)
        q.request_requirements_confirmation(conn, project_id=ctx.pid)
        q.pdr_status(conn, project_id=ctx.pid)
        assert scalar(conn, "SELECT count(*) FROM approval") == 0

    def test_an_edit_after_approval_reopens_the_gate(self, conn):
        ctx = scoped(conn)
        assert "candidates" in q.search_for_requirement(conn, req_id=ctx.need_id)
        record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[1], "text": "Reworded after approval"}])
        refused = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert refused["refused"] and "not confirmed in their current form" in refused["reason"]

    def test_changing_search_terms_reopens_the_gate(self, conn):
        """
        The search terms decide what the registry is asked, so they are part
        of what the human confirms. When they were left out of the confirmed
        content, an agent could confirm 'oauth', swap the terms for
        something that matches nothing, sweep, and get a clean cold-start
        BUILD on a need the registry could have answered.
        """
        make_capability(conn)
        ctx = scoped(conn)
        record_need(conn, ctx.pid, search_terms=["zzz-matches-nothing"])
        refused = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert refused["refused"] and refused["gate"] == "requirements_confirmation"
        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert rc["status"] == "awaiting_approval"
        assert "zzz-matches-nothing" in rc["summary_markdown"]   # the human sees the swap

    def test_changing_problem_types_reopens_the_gate(self, conn):
        ctx = scoped(conn)
        record_need(conn, ctx.pid, problem_types=["something-else"])
        assert q.search_for_requirement(conn, req_id=ctx.need_id)["refused"]

    def test_rejection_is_reported_with_its_note(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        r = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        reject(conn, r["approval_request_id"], note="dark mode is in scope")
        again = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert again["status"] == "rejected"
        assert again["rejection_note"] == "dark mode is in scope"

    def test_one_decision_per_request(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        r = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        approve(conn, r["approval_request_id"])
        second = pdr_approvals.record_approval(
            conn, request_id=r["approval_request_id"], outcome="rejected",
            approver="someone", note="changed my mind",
        )
        assert "already approved" in second["error"]

    def test_approval_rows_cannot_be_rewritten(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        r = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        approve(conn, r["approval_request_id"], approver="ricky@desk")
        with conn.cursor() as cur:
            cur.execute("UPDATE approval SET approver = 'the-llm', outcome = 'rejected'")
            cur.execute("UPDATE approval_request SET subject_hash = 'tampered'")
        conn.commit()
        assert scalar(conn, "SELECT approver FROM approval") == "ricky@desk"
        assert scalar(conn, "SELECT subject_hash FROM approval_request") != "tampered"


# ---------------------------------------------------------------------------
# 5. search_for_requirement — proof of search, cold start, recall (gaps 2, 14)
# ---------------------------------------------------------------------------

class TestSearch:

    def test_runs_on_needs_not_requirements(self, conn):
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.reqs["staff-sign-in"])
        assert r["refused"] and "capability needs" in r["reason"]
        assert q.search_for_requirement(conn, req_id=str(uuid.uuid4())) is None

    def test_an_empty_search_is_still_logged(self, conn):
        """The v1.0 gate counted fit rows, so a search that found nothing left no proof."""
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert r["candidate_count"] == 0
        assert r["coverage_outcome"] == "no_registry_coverage"
        assert r["counts_as_mandatory_sweep"] is True
        with conn.cursor() as cur:
            cur.execute("SELECT query_source, queries, found_count, registry_size "
                        "FROM requirement_search WHERE id = %s", (r["search_id"],))
            assert cur.fetchone() == ("need_terms", ["oauth", "oidc login"], 0, 0)

    def test_sweep_runs_every_search_term(self, conn):
        make_capability(conn, "oauth-library", "OAuth Library")
        make_capability(conn, "oidc-login-kit", "OIDC Login Kit")
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert {c["normalized_key"] for c in r["candidates"]} == {
            "pypi:oauth-library", "pypi:oidc-login-kit",
        }
        assert scalar(conn, "SELECT count(*) FROM fit_evaluation "
                            "WHERE project_requirement_id = %s", (ctx.need_id,)) == 2

    def test_custom_or_filtered_search_is_not_the_sweep(self, conn):
        make_capability(conn)
        ctx = scoped(conn)
        custom = q.search_for_requirement(conn, req_id=ctx.need_id, search_query="oauth")
        filtered = q.search_for_requirement(conn, req_id=ctx.need_id, ecosystem="npm")
        assert custom["counts_as_mandatory_sweep"] is False
        assert filtered["counts_as_mandatory_sweep"] is False
        assert filtered["candidate_count"] == 0
        refused = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="x")
        assert refused["refused"] and refused["gate"] == "search_before_decision"

    def test_candidate_without_a_source_binding_is_reported_not_dropped(self, conn):
        make_capability(conn, bind=False)
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert r["candidate_count"] == 0
        assert r["unevaluable"][0]["normalized_key"] == "pypi:oauth-library"
        assert "source binding" in r["unevaluable"][0]["reason"]

    def test_recalls_banked_evaluations_by_problem_type(self, conn):
        record_evaluation(
            conn, asset_slug="acme-authkit", verdict="Reference", banked=True,
            evidence_depth="docs_only", summary="Token refresh done well.",
            problem_types=["oauth-flow"], url="https://github.com/acme/authkit",
        )
        record_evaluation(
            conn, asset_slug="junk-oauth", verdict="Reject", banked=False,
            evidence_depth="metadata_only", summary="An oauth wrapper.",
            problem_types=["something-else"],
        )
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.need_id)
        by_slug = {m["asset_slug"]: m for m in r["evaluation_matches"]}
        assert by_slug["acme-authkit"]["match"] == "problem_type"
        assert by_slug["acme-authkit"]["counts_as_banked_match"] is True
        assert by_slug["acme-authkit"]["evidence_depth"] == "docs_only"
        # A text-only hit is shown but is too weak to flag a decision on.
        assert by_slug["junk-oauth"]["match"] == "text"
        assert by_slug["junk-oauth"]["counts_as_banked_match"] is False
        assert r["coverage_outcome"] == "banked_only"

    def test_a_text_only_match_never_counts_however_good_the_asset(self, conn):
        """
        Only a problem-type tag match is strong enough to flag a decision.
        A banked, adoptable asset that merely mentions the search term is
        shown to the reader and nothing more.
        """
        record_evaluation(
            conn, asset_slug="acme-everything", verdict="Adopt", banked=True,
            evidence_depth="code_read", code_quality="solid",
            summary="A big toolkit; includes an oauth helper somewhere.",
            problem_types=["http-client"],
        )
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.need_id)
        match = r["evaluation_matches"][0]
        assert match["match"] == "text" and match["counts_as_banked_match"] is False
        assert r["coverage_outcome"] == "no_registry_coverage"
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["flags"] == ["cold_start_build"]

    def test_links_an_evaluation_to_its_ingested_capability(self, conn):
        cap_id, _ = make_capability(conn, html_url="https://github.com/Acme/AuthKit/")
        record_evaluation(
            conn, asset_slug="acme-authkit", verdict="Adopt", banked=True,
            evidence_depth="code_read", code_quality="solid",
            problem_types=["oauth-flow"], url="https://github.com/acme/authkit.git",
        )
        ctx = scoped(conn)
        match = q.search_for_requirement(conn, req_id=ctx.need_id)["evaluation_matches"][0]
        assert match["selectable"] is True and match["linked_capability_id"] == cap_id

    def test_search_rows_are_append_only(self, conn):
        ctx = scoped(conn)
        r = q.search_for_requirement(conn, req_id=ctx.need_id)
        with conn.cursor() as cur:
            cur.execute("UPDATE requirement_search SET found_count = 99")
        conn.commit()
        assert scalar(conn, "SELECT found_count FROM requirement_search WHERE id = %s",
                      (r["search_id"],)) == 0


# ---------------------------------------------------------------------------
# 6. record_decision — CIP computes; the LLM may propose      (gaps 2, 3, 5)
# ---------------------------------------------------------------------------

class TestDecision:

    def test_no_decision_without_a_sweep(self, conn):
        ctx = scoped(conn)
        r = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="x")
        assert r["refused"] and r["gate"] == "search_before_decision"
        assert scalar(conn, "SELECT count(*) FROM decision_record") == 0
        assert scalar(conn, "SELECT count(*) FROM recommendation") == 0

    def test_cold_start_build_is_allowed_and_labelled(self, conn):
        """v1.0 refused BUILD forever on an empty registry."""
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "BUILD" and d["decided_by"] == "cip_rules"
        assert d["coverage_outcome"] == "no_registry_coverage"
        assert d["flags"] == ["cold_start_build"] and d["listed_for_human"] is True
        # Step 13's own proof-of-search sentinel is in place.
        assert scalar(conn, "SELECT rule_name FROM recommendation "
                            "WHERE project_requirement_id = %s",
                      (ctx.need_id,)) == "__no_candidates__"

    def test_cip_cannot_decide_without_constraints(self, conn):
        make_capability(conn)
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        r = q.record_decision(conn, req_id=ctx.need_id)
        assert r["refused"] and "cannot compute a verdict" in r["reason"]
        # A refused decision leaves nothing behind.
        assert scalar(conn, "SELECT count(*) FROM recommendation") == 0
        assert scalar(conn, "SELECT count(*) FROM decision_record") == 0

    def test_llm_judgment_is_recorded_as_such(self, conn):
        _, cv = make_capability(conn)
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        assert q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                                 chosen_capability_version_id=cv)["refused"]   # no rationale
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=cv, rationale="maintained, fits")
        assert d["decided_by"] == "llm_judgment" and d["computed_verdict"] is None
        assert d["pinned_revision"] == "rev-oauth-library"
        assert d["listed_for_human"] is True

    def test_agreeing_with_cip_is_a_cip_decision(self, conn):
        ctx = decided_adopt(conn)
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=ctx.cv_id)
        assert d["decided_by"] == "cip_rules" and d["computed_verdict"] == "ADOPT"
        assert d["computed_rule"] == "strong_adopt" and d["listed_for_human"] is False

    def test_disagreeing_is_an_override_and_needs_a_rationale(self, conn):
        ctx = decided_adopt(conn)
        assert q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD")["refused"]
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                              rationale="we need behaviour the library does not expose")
        assert d["decided_by"] == "llm_override" and d["computed_verdict"] == "ADOPT"
        assert d["verdict"] == "BUILD"

    def test_override_of_a_blocking_gap_is_flagged(self, conn):
        _, gpl = make_capability(conn, "oauth-gpl", "OAuth GPL", license="GPL-3.0")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=gpl,
                              rationale="internal tool, never distributed")
        assert d["decided_by"] == "llm_override"
        assert "overrides_blocking_gap" in d["flags"]

    def test_a_metadata_only_candidate_is_not_a_cip_decision(self, conn):
        """
        Most of the registry is ingested from metadata: a license field, no
        analysed source. The fit matchers read evidence, so against such a
        candidate a license allowlist reports "no identified license" as a
        blocking gap. CIP must not turn that into a verdict.
        """
        _, cv = make_capability(conn, metadata_license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        search = q.search_for_requirement(conn, req_id=ctx.need_id)
        cand = search["candidates"][0]
        assert cand["evidence"] == "insufficient" and cand["missing_evidence"] == ["license"]
        assert cand["blocking_gap_count"] == 1
        assert "no analysed evidence" in search["note"]

        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "holds no analysed evidence" in refused["reason"]

        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=cv,
                              rationale="MIT per the repo; widely used")
        assert d["decided_by"] == "llm_judgment" and d["computed_verdict"] is None
        # The gap is missing evidence, so it is reported as that, not as a
        # blocking gap someone chose to override.
        assert d["flags"] == ["unanalysed_candidate", "judgment:insufficient_evidence"]
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "unanalysed_candidate" in {f["code"] for f in first["human_approval_items"]}

    def test_an_analysed_winner_keeps_cips_verdict_when_others_are_metadata_only(self, conn):
        _, analysed = make_capability(conn, license="MIT")
        _, meta = make_capability(conn, "oauth-meta", "OAuth Meta", metadata_license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert (d["verdict"], d["decided_by"]) == ("ADOPT", "cip_rules")
        assert d["chosen_capability_version_id"] == analysed
        # Preferring the unanalysed one is an override of a real finding.
        o = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=meta, rationale="smaller")
        assert o["decided_by"] == "llm_override" and o["flags"] == ["unanalysed_candidate"]

    def test_only_searched_candidates_can_be_chosen(self, conn):
        make_capability(conn, license="MIT")
        _, other = make_capability(conn, "pdf-kit", "PDF Kit", license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        r = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=other, rationale="prefer it")
        assert r["refused"] and "was not evaluated by a search" in r["reason"]

    def test_reject_is_not_a_need_outcome(self, conn):
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        r = q.record_decision(conn, req_id=ctx.need_id, verdict="REJECT", rationale="x")
        assert r["refused"] and "retire" in r["reason"]
        assert q.record_decision(conn, req_id=ctx.need_id, verdict="YOLO")["refused"]

    def test_build_despite_a_banked_match_is_flagged(self, conn):
        record_evaluation(conn, asset_slug="acme-authkit", verdict="Reference", banked=True,
                          evidence_depth="docs_only", problem_types=["oauth-flow"])
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["coverage_outcome"] == "banked_only"
        assert d["flags"] == ["build_despite_banked_match"]

    def test_changing_the_need_voids_earlier_searches(self, conn):
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        record_need(conn, ctx.pid, search_terms=["sso"])
        # The edit re-opens gate 1 first...
        r = q.record_decision(conn, req_id=ctx.need_id)
        assert r["refused"] and r["gate"] == "requirements_confirmation"
        # ...and once the human has confirmed the new terms, the sweep that
        # ran on the old ones still does not count.
        confirm(conn, ctx.pid)
        r = q.record_decision(conn, req_id=ctx.need_id)
        assert r["refused"] and r["gate"] == "search_before_decision"

    def test_a_refused_redecision_does_not_revoke_build_authorisation(self, conn):
        """
        Re-recommending deletes the recommendation row and cascades away its
        build_authorization. A decision that is then refused must not have
        done that.
        """
        ctx = locked_build(conn)
        assert can_generate_for(conn, project_requirement_id=uuid.UUID(ctx.need_id)).allowed
        r = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=str(uuid.uuid4()),
                              rationale="try something else")
        assert r["refused"]
        assert can_generate_for(conn, project_requirement_id=uuid.UUID(ctx.need_id)).allowed

    def test_decision_log_is_append_only_and_history_is_kept(self, conn):
        ctx = decided_adopt(conn)
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="changed")
        assert scalar(conn, "SELECT count(*) FROM decision_record") == 2
        with conn.cursor() as cur:
            cur.execute("UPDATE decision_record SET final_verdict = 'ADOPT', "
                        "decided_by = 'cip_rules'")
        conn.commit()
        assert scalar(conn, "SELECT count(*) FROM decision_record "
                            "WHERE final_verdict = 'BUILD'") == 1

    def test_requirements_cannot_be_decided_directly(self, conn):
        ctx = scoped(conn)
        r = q.record_decision(conn, req_id=ctx.reqs["todays-bookings"], verdict="BUILD")
        assert r["refused"]
        assert q.record_decision(conn, req_id=str(uuid.uuid4()), verdict="BUILD") is None


# ---------------------------------------------------------------------------
# 7. lock_architecture — human gate 2                          (gaps 4, 5, 9)
# ---------------------------------------------------------------------------

LOCK_INPUTS = dict(
    architecture_doc_text="# Architecture\nFastAPI and Postgres.\n",
    approved_providers=[{"name": "Google", "category": "auth", "purpose": "staff sign-in",
                         "cost": "free", "data_leaves_infra": True}],
    cost_commitments=[{"item": "Hosting", "kind": "recurring", "amount": "5",
                       "currency": "usd", "period": "month"}],
    project_decisions=[{"key": "database", "category": "database",
                        "choice": "PostgreSQL 16", "rationale": "already in use"}],
    unresolved_risks=["Google Workspace admin consent not yet confirmed"],
)


class TestLock:

    def test_refuses_with_undecided_needs(self, conn):
        ctx = scoped(conn)
        r = q.lock_architecture(conn, project_id=ctx.pid)
        assert r["refused"] and any("oauth-login" in b for b in r["blockers"])
        assert scalar(conn, "SELECT count(*) FROM approval_request "
                            "WHERE kind = 'architecture_lock'") == 0

    def test_first_call_asks_and_writes_no_lock(self, conn):
        ctx = decided_build(conn)
        r = q.lock_architecture(conn, project_id=ctx.pid, **LOCK_INPUTS)
        assert r["status"] == "awaiting_approval"
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0
        codes = {f["code"] for f in r["human_approval_items"]}
        assert {"cold_start_build", "sensitive_provider", "data_leaves_infra",
                "cost_commitment", "unresolved_risk", "bespoke_requirements"} <= codes

    def test_the_caller_cannot_name_the_approver(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_by="the-llm")
        assert first["status"] == "awaiting_approval"          # still needs a human
        assert first["warnings"]
        approve(conn, first["approval_request_id"], approver="ricky@desk")
        second = q.lock_architecture(conn, project_id=ctx.pid, approved_by="the-llm")
        assert second["approved_by"] == "ricky@desk"
        assert scalar(conn, "SELECT approved_by FROM architecture_lock") == "ricky@desk"

    def test_approval_is_bound_to_the_exact_payload(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, **LOCK_INPUTS)
        approve(conn, first["approval_request_id"])
        # Same call with one more provider is a different payload: not approved.
        changed = {**LOCK_INPUTS, "approved_providers": LOCK_INPUTS["approved_providers"] + [
            {"name": "Stripe", "category": "payment", "purpose": "deposits",
             "cost": "usage", "data_leaves_infra": True}]}
        r = q.lock_architecture(conn, project_id=ctx.pid, **changed)
        assert r["status"] == "awaiting_approval"
        assert r["approval_request_id"] != first["approval_request_id"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0

    def test_lock_records_everything_that_was_approved(self, conn):
        ctx = decided_build(conn)
        res = lock(conn, ctx.pid, **LOCK_INPUTS)
        assert res["version"] == 1 and res["build_authorizations"] == 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approval_id, payload_hash, requirements_hash, "
                "architecture_doc_hash, approved_providers, project_decisions, scope, "
                "decisions FROM architecture_lock WHERE id = %s",
                (res["architecture_lock_id"],),
            )
            (approval_id, payload_hash, req_hash, doc_hash,
             providers, proj, scope, decisions) = cur.fetchone()
        assert approval_id is not None and payload_hash == res["payload_hash"]
        assert req_hash and doc_hash
        assert providers[0]["name"] == "Google" and proj[0]["key"] == "database"
        assert scope["bespoke"] == ["todays-bookings"] and scope["deferred"] == ["dark-mode"]
        assert decisions[0]["need_slug"] == "oauth-login"
        assert scalar(conn, "SELECT count(*) FROM decision_record WHERE id = %s",
                      (decisions[0]["decision_record_id"],)) == 1
        # The approval that unlocked it is for this payload hash.
        assert scalar(conn, "SELECT ar.subject_hash FROM approval a JOIN approval_request ar "
                            "ON ar.id = a.approval_request_id WHERE a.id = %s",
                      (approval_id,)) == payload_hash

    def test_build_needs_are_authorised_in_the_approvers_name(self, conn):
        ctx = decided_build(conn)
        assert not can_generate_for(conn, project_requirement_id=uuid.UUID(ctx.need_id)).allowed
        lock(conn, ctx.pid)
        assert can_generate_for(conn, project_requirement_id=uuid.UUID(ctx.need_id)).allowed
        assert scalar(conn, "SELECT authorized_by FROM build_authorization") == "ricky@desk"

    def test_the_llm_cannot_authorise_a_locked_need_itself(self, conn):
        ctx = decided_build(conn)
        rec = scalar(conn, "SELECT id FROM recommendation WHERE project_requirement_id = %s",
                     (ctx.need_id,))
        r = queries.authorize_build(conn, recommendation_id=str(rec),
                                    authorized_by="the-llm", reason="go")
        assert "lock_architecture" in r["error"]
        assert scalar(conn, "SELECT count(*) FROM build_authorization") == 0

    def test_an_approved_override_becomes_the_effective_verdict(self, conn):
        ctx = decided_adopt(conn)
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                          rationale="need behaviour the library lacks")
        # Until the lock, the recommendation row still holds what CIP computed.
        assert scalar(conn, "SELECT verdict FROM recommendation") == "ADOPT"
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "llm_override" in {f["code"] for f in first["human_approval_items"]}
        approve(conn, first["approval_request_id"])
        q.lock_architecture(conn, project_id=ctx.pid)
        with conn.cursor() as cur:
            cur.execute("SELECT verdict, rule_name, chosen_capability_version_id "
                        "FROM recommendation")
            assert cur.fetchone() == ("BUILD", "__llm_override__", None)
        assert can_generate_for(conn, project_requirement_id=uuid.UUID(ctx.need_id)).allowed

    def test_relock_is_a_change_request_with_a_diff(self, conn):
        ctx = locked_adopt(conn)
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="changed plan")
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert first["status"] == "awaiting_approval" and first["version"] == 2
        change = first["changes_from_active_lock"][0]
        assert change["kind"] == "decision_changed" and change["subject"] == "oauth-login"
        assert "ADOPT" in change["detail"] and "BUILD" in change["detail"]
        approve(conn, first["approval_request_id"])
        second = q.lock_architecture(conn, project_id=ctx.pid)
        assert second["version"] == 2
        with conn.cursor() as cur:
            cur.execute("SELECT version, status FROM architecture_lock ORDER BY version")
            assert cur.fetchall() == [(1, "superseded"), (2, "active")]

    def test_locking_the_same_content_again_is_a_no_op(self, conn):
        ctx = locked_build(conn)
        again = q.lock_architecture(conn, project_id=ctx.pid)
        assert again["unchanged"] is True and again["version"] == 1
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 1

    def test_a_rejected_lock_stays_rejected(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        reject(conn, first["approval_request_id"], note="use a library for auth")
        r = q.lock_architecture(conn, project_id=ctx.pid)
        assert r["refused"] and "use a library for auth" in r["reason"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0

    def test_a_lock_row_cannot_be_edited(self, conn):
        ctx = locked_build(conn)
        for stmt in (
            "UPDATE architecture_lock SET decisions = '[]'::jsonb",
            "UPDATE architecture_lock SET approved_by = 'the-llm'",
            "UPDATE architecture_lock SET payload_hash = 'x'",
            "UPDATE architecture_lock SET version = 9",
            "UPDATE architecture_lock SET status = 'active' WHERE status = 'superseded'",
        ):
            if "superseded" in stmt:
                with conn.cursor() as cur:
                    cur.execute("UPDATE architecture_lock SET status = 'superseded'")
                conn.commit()
            with pytest.raises(psycopg.errors.RaiseException):
                with conn.cursor() as cur:
                    cur.execute(stmt)
            conn.rollback()
        assert scalar(conn, "SELECT approved_by FROM architecture_lock") == "ricky@desk"

    def test_brief_and_lock_agree(self, conn):
        ctx = decided_build(conn)
        brief = q.build_approval_brief(conn, project_id=ctx.pid, **LOCK_INPUTS)
        assert brief["ready_to_lock"] is True and brief["approval_state"] == "none"
        first = q.lock_architecture(conn, project_id=ctx.pid, **LOCK_INPUTS)
        assert first["payload_hash"] == brief["payload_hash"]
        assert first["summary_markdown"] == brief["brief_markdown"]
        # The brief is a view: it opened no request of its own.
        assert scalar(conn, "SELECT count(*) FROM approval_request "
                            "WHERE kind = 'architecture_lock'") == 1

    def test_bad_lock_inputs_are_blockers(self, conn):
        ctx = decided_build(conn)
        r = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[
            {"name": "Mystery", "category": "quantum", "cost": "free"}])
        assert r["refused"]
        assert any("category" in b for b in r["blockers"])
        assert any("data_leaves_infra" in b for b in r["blockers"])
        assert q.lock_architecture(conn, project_id=str(uuid.uuid4())) is None


# ---------------------------------------------------------------------------
# 8. check_against_lock — structured, fail-closed, logged          (gap 8)
# ---------------------------------------------------------------------------

class TestCheckAgainstLock:

    def test_no_lock_blocks(self, conn):
        ctx = scoped(conn)
        r = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "Auth0"}]})
        assert r["verdict"] == "blocked"

    def test_a_description_alone_cannot_be_checked(self, conn):
        ctx = locked_build(conn)
        r = q.check_against_lock(conn, project_id=ctx.pid,
                                 proposed_change={"description": "Internal refactor"})
        assert r["verdict"] == "blocked" and "no checkable content" in r["reason"]

    def test_paths_are_held_to_the_task(self, conn):
        ctx = locked_build(conn)
        task = bespoke_task(conn, ctx)["build_task_id"]
        inside = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": task, "changed_paths": ["src/bookings/today.py",
                                                     "tests/test_bookings.py"]})
        outside = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": task, "changed_paths": ["src/billing/invoice.py"]})
        escape = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": task, "changed_paths": ["src/bookings/../../etc/passwd"]})
        no_task = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "changed_paths": ["src/bookings/today.py"]})
        assert inside["verdict"] == "allowed"
        assert outside["verdict"] == "needs_approval"
        assert outside["cited"][0]["permitted_paths"] == ["src/bookings/", "tests/test_bookings.py"]
        assert escape["verdict"] == "needs_approval"
        assert no_task["verdict"] == "needs_approval"

    def test_swapping_a_locked_capability_cites_the_decision(self, conn):
        ctx = locked_adopt(conn)
        r = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "capability_changes": [{"need": "oauth-login",
                                    "capability_version_id": str(uuid.uuid4())}]})
        assert r["verdict"] == "needs_approval"
        assert r["cited"][0]["need_slug"] == "oauth-login"
        assert r["cited"][0]["capability_version_id"] == ctx.cv_id
        same = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "capability_changes": [{"need": "oauth-login",
                                    "capability_version_id": ctx.cv_id}]})
        assert same["verdict"] == "allowed"

    def test_providers_and_dependencies(self, conn):
        ctx = decided_adopt(conn)
        lock(conn, ctx.pid, **LOCK_INPUTS)
        ok = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "google", "action": "add"}],
            "dependency_adds": [{"ecosystem": "pypi", "name": "oauth-library"}]})
        new_provider = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "Auth0", "action": "add"}]})
        drop = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "Google", "action": "remove"}]})
        new_dep = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "dependency_adds": [{"ecosystem": "pypi", "name": "requests"}]})
        assert ok["verdict"] == "allowed"
        assert new_provider["verdict"] == "needs_approval"
        assert drop["verdict"] == "needs_approval" and drop["cited"][0]["name"] == "Google"
        assert new_dep["verdict"] == "needs_approval"
        assert "Search-before-build applies to dependencies" in new_dep["reason"]

    def test_a_stale_lock_cannot_allow_anything(self, conn):
        ctx = locked_build(conn)
        record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[1], "text": "Changed after the lock"}])
        r = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "Auth0", "action": "use"}]})
        assert r["verdict"] == "needs_approval"
        assert any("requirements have changed" in x for x in r["reasons"])

    def test_every_check_is_logged(self, conn):
        ctx = locked_build(conn)
        task = bespoke_task(conn, ctx)["build_task_id"]
        q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": task, "changed_paths": ["src/billing/x.py"]})
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM audit_event WHERE event_type = 'lock_check'")
            payload = cur.fetchone()[0]
        assert payload["verdict"] == "needs_approval" and payload["build_task_id"] == task
        assert payload["change"]["changed_paths"] == ["src/billing/x.py"]

    def test_a_v1_0_lock_is_not_trusted(self, conn):
        ctx = decided_build(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO architecture_lock (project_id, version, approved_by, "
                "decisions, source_lock_hash) VALUES (%s, 1, 'engineer', '[]'::jsonb, 'x')",
                (ctx.pid,),
            )
        conn.commit()
        r = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "Auth0"}]})
        assert r["verdict"] == "needs_approval" and "before v1.1" in r["reason"]
        refused = bespoke_task(conn, ctx)
        assert refused["refused"] and "before v1.1" in refused["reason"]
        # Relocking replaces it with an approved lock.
        res = lock(conn, ctx.pid)
        assert res["version"] == 2
        assert "architecture_lock_id" in res and bespoke_task(conn, ctx)["status"] == "planned"


# ---------------------------------------------------------------------------
# 9. record_build_progress — the build gate and verification    (gaps 5, 7)
# ---------------------------------------------------------------------------

class TestBuildGate:

    def test_no_task_without_a_lock(self, conn):
        ctx = decided_build(conn)
        r = bespoke_task(conn, ctx)
        assert r["refused"] and "No active architecture lock" in r["reason"]
        assert scalar(conn, "SELECT count(*) FROM build_task") == 0

    def test_creates_a_task_under_the_active_lock(self, conn):
        ctx = locked_build(conn)
        r = bespoke_task(conn, ctx)
        assert r["status"] == "planned" and r["lock_version"] == 1
        assert r["architecture_lock_id"] == ctx.lock["architecture_lock_id"]

    def test_permitted_paths_are_required(self, conn):
        ctx = locked_build(conn)
        assert "permitted_paths is required" in bespoke_task(conn, ctx, permitted_paths=[])["reason"]
        assert bespoke_task(conn, ctx, permitted_paths=["../outside"])["refused"]

    def test_out_of_scope_requirements_cannot_be_built(self, conn):
        ctx = locked_build(conn)
        deferred = bespoke_task(conn, ctx, req_ids=["dark-mode"])
        assert deferred["refused"] and "deferred" in deferred["reason"]
        assert "error" in bespoke_task(conn, ctx, req_ids=["no-such-thing"])
        need_only = bespoke_task(conn, ctx, req_ids=["oauth-login"])
        assert "at least one PDR requirement" in need_only["reason"]

    def test_a_build_need_goes_through_step_13(self, conn):
        ctx = locked_build(conn)
        args = dict(req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"],
                    title="Sign-in", objective="Build OAuth sign-in")
        assert bespoke_task(conn, ctx, **args)["status"] == "planned"
        # Take away the Step 13 authorisation: the lock alone is not enough.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM build_authorization")
        conn.commit()
        r = bespoke_task(conn, ctx, **args)
        assert r["refused"] and "Search-before-build gate refuses 'oauth-login'" in r["reason"]

    def test_a_reuse_need_pins_the_task_to_the_locked_version(self, conn):
        ctx = locked_adopt(conn)
        args = dict(req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"],
                    title="Sign-in", objective="Integrate the OAuth library")
        wrong = bespoke_task(conn, ctx, reused_capability_version_id=str(uuid.uuid4()), **args)
        assert wrong["refused"] and ctx.cv_id in wrong["reason"]
        right = bespoke_task(conn, ctx, reused_capability_version_id=ctx.cv_id, **args)
        assert right["status"] == "planned"
        # Left out, CIP fills it from the lock and says so.
        filled = bespoke_task(conn, ctx, **args)
        assert filled["reused_capability_version_id"] == ctx.cv_id
        assert scalar(conn, "SELECT reused_capability_version_id::text FROM build_task "
                            "WHERE id = %s", (filled["build_task_id"],)) == ctx.cv_id

    def test_the_pin_cannot_be_dodged_by_linking_only_the_requirement(self, conn):
        """
        Reviewer's bypass: link 'staff-sign-in' and not the need behind it,
        and the task could name any capability version.
        """
        ctx = locked_adopt(conn)
        args = dict(req_ids=["staff-sign-in"], permitted_paths=["src/auth/"],
                    title="Sign-in", objective="Integrate the OAuth library")
        _, other_cv = make_capability(conn, name="other-lib", display="Other Lib")
        wrong = bespoke_task(conn, ctx, reused_capability_version_id=other_cv, **args)
        assert wrong["refused"] and "is not what the lock pinned" in wrong["reason"]
        assert bespoke_task(conn, ctx, **args)["reused_capability_version_id"] == ctx.cv_id

    def test_a_requirement_cannot_start_before_its_needs_are_locked(self, conn):
        ctx = locked_build(conn)
        # A second need appears after the lock. The scope changed, so the lock
        # is stale and nothing new may start until it is reconfirmed and relocked.
        record_need(conn, ctx.pid, slug="session-store", description="Server-side sessions",
                    search_terms=["session store"])
        r = bespoke_task(conn, ctx, req_ids=["staff-sign-in"], permitted_paths=["src/auth/"])
        assert r["refused"] and "stale" in r["reason"]

    def test_a_changed_decision_closes_the_gate_until_relock(self, conn):
        ctx = locked_adopt(conn)
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="changed plan")
        r = bespoke_task(conn, ctx, req_ids=["staff-sign-in", "oauth-login"],
                         permitted_paths=["src/auth/"],
                         reused_capability_version_id=ctx.cv_id)
        assert r["refused"] and "has changed since it was locked" in r["reason"]
        # Work that does not rest on that need is unaffected.
        assert bespoke_task(conn, ctx)["status"] == "planned"

    def test_a_task_from_an_older_lock_goes_stale_when_its_decision_changes(self, conn):
        ctx = locked_adopt(conn)
        task = bespoke_task(conn, ctx, req_ids=["staff-sign-in", "oauth-login"],
                            permitted_paths=["src/auth/"],
                            reused_capability_version_id=ctx.cv_id)["build_task_id"]
        other = bespoke_task(conn, ctx)["build_task_id"]
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="changed plan")
        held = q.record_build_progress(conn, build_task_id=task, status="in_progress")
        assert held["refused"] and "changed since it was locked" in held["reason"]
        relock = lock(conn, ctx.pid)                             # v2
        # The relock ends the task whose decision changed, and says so.
        assert relock["tasks_abandoned"] == [task]
        stale = q.record_build_progress(conn, build_task_id=task, status="in_progress")
        assert stale["refused"] and "abandoned" in stale["reason"]
        assert scalar(conn, "SELECT payload->>'reason' FROM audit_event "
                            "WHERE event_type = 'task_updated' AND subject_id = %s "
                            "ORDER BY seq DESC LIMIT 1", (task,)) == "not carried by lock v2"
        # A task whose decisions did not change carries over to v2.
        assert q.record_build_progress(conn, build_task_id=other,
                                       status="in_progress")["status"] == "in_progress"

    def test_refusals_are_audited(self, conn):
        ctx = locked_build(conn)
        bespoke_task(conn, ctx, req_ids=["dark-mode"])
        assert scalar(conn, "SELECT count(*) FROM audit_event "
                            "WHERE event_type = 'task_refused'") == 1


class TestVerification:

    def _task(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        q.record_build_progress(conn, build_task_id=tid, status="implemented",
                                target_commit="abc1234def")
        return ctx, tid

    def test_status_moves_in_order(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        skip = q.record_build_progress(conn, build_task_id=tid, status="verified")
        assert "cannot move a task from 'planned' to 'verified'" in skip["error"]
        assert "error" in q.record_build_progress(conn, build_task_id=tid, status="done")
        assert "error" in q.record_build_progress(conn, build_task_id=str(uuid.uuid4()),
                                                  status="in_progress")

    def test_verified_needs_a_pass_for_every_criterion(self, conn):
        """v1.0 let a task be set to verified with no verification row at all."""
        ctx, tid = self._task(conn)
        r = q.record_build_progress(conn, build_task_id=tid, status="verified")
        assert r["refused"] and "no verification recorded" in r["reason"]
        assert scalar(conn, "SELECT status FROM build_task WHERE id = %s", (tid,)) == "implemented"

    def test_a_pass_must_say_what_ran_and_against_what(self, conn):
        ctx, tid = self._task(conn)
        bare = q.record_build_progress(conn, build_task_id=tid, verification={
            "req_id": "todays-bookings", "criterion_ordinal": 1, "outcome": "pass"})
        for word in ("commit_sha", "test_ref", "command", "exit_code"):
            assert word in bare["error"]
        nonzero = q.record_build_progress(conn, build_task_id=tid, verification={
            **PASS, "req_id": "todays-bookings", "criterion_ordinal": 1, "exit_code": 1})
        assert "exit_code 0" in nonzero["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0

    def test_pass_then_verified(self, conn):
        ctx, tid = self._task(conn)
        r = q.record_build_progress(conn, build_task_id=tid, status="verified", verification={
            **PASS, "req_id": "todays-bookings", "criterion_ordinal": 1})
        assert r["status"] == "verified"
        assert r["verifications"][0]["recorded_by"] == "agent_reported"
        with conn.cursor() as cur:
            cur.execute("SELECT commit_sha, test_ref, exit_code, acceptance_criterion_id "
                        "IS NOT NULL FROM requirement_verification")
            assert cur.fetchone() == ("abc1234def", PASS["test_ref"], 0, True)

    def test_a_fail_after_a_pass_unverifies(self, conn):
        ctx, tid = self._task(conn)
        q.record_build_progress(conn, build_task_id=tid, status="verified", verification={
            **PASS, "req_id": "todays-bookings", "criterion_ordinal": 1})
        r = q.record_build_progress(conn, build_task_id=tid, verification={
            "req_id": "todays-bookings", "criterion_ordinal": 1, "outcome": "fail"})
        assert r["status"] == "implemented" and "back to implemented" in r["note"]
        cov = q.coverage(conn, project_id=ctx.pid)
        assert cov["progress"]["verified"] == 0

    def test_a_task_can_only_verify_what_it_delivers(self, conn):
        ctx, tid = self._task(conn)
        r = q.record_build_progress(conn, build_task_id=tid, verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1})
        assert "does not belong to a requirement this task links" in r["error"]

    def test_a_refused_verified_keeps_the_valid_evidence(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx, req_ids=["staff-sign-in", "oauth-login"],
                           permitted_paths=["src/auth/"])["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        r = q.record_build_progress(conn, build_task_id=tid, status="verified", verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1})
        assert r["refused"] and "staff-sign-in #2" in r["reason"]
        assert r["status"] == "implemented"
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 1

    def test_database_refuses_a_pass_without_evidence(self, conn):
        ctx, tid = self._task(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO requirement_verification "
                    "(project_requirement_id, build_task_id, outcome) "
                    "VALUES (%s, %s, 'pass')",
                    (ctx.reqs["todays-bookings"], tid),
                )
        conn.rollback()

    def test_cip_executed_evidence_is_recorded_as_such(self, conn):
        ctx, tid = self._task(conn)
        r = q.record_executed_verification(conn, build_task_id=tid, verification={
            **PASS, "req_id": "todays-bookings", "criterion_ordinal": 1,
            "output_digest": "d" * 64, "worktree_dirty": False})
        assert r["recorded_by"] == "cip_executed" and r["remaining"] == []
        cov = q.coverage(conn, project_id=ctx.pid)
        assert cov["progress"]["verified_cip_executed"] == 1


# ---------------------------------------------------------------------------
# 10. coverage + pdr_status — honest denominators, resumable  (gaps 6, 10, 12)
# ---------------------------------------------------------------------------

class TestCoverage:

    def test_empty_project(self, conn):
        ing = ingest(conn)
        cov = q.coverage(conn, project_id=ing["project_id"])
        assert cov["total_requirements"] == 0 and cov["progress"]["verified_pct"] is None
        assert q.coverage(conn, project_id=str(uuid.uuid4())) is None

    def test_denominators_are_explicit(self, conn):
        ctx = locked_build(conn)
        cov = q.coverage(conn, project_id=ctx.pid)
        assert [s["heading"] for s in cov["pdr"]["unmapped_sections"]] == ["Goals", "Glossary"]
        assert cov["requirements"]["total"] == 4
        assert cov["requirements"]["by_disposition"] == {
            "bespoke": 1, "deferred": 1, "not_applicable": 1, "via_needs": 1}
        # Deferred and not-applicable requirements are not in the denominator.
        assert cov["progress"]["in_scope"] == cov["total_requirements"] == 2
        assert cov["progress"]["decided"] == 2 and cov["progress"]["locked"] == 2
        assert cov["needs"]["by_coverage_outcome"] == {"no_registry_coverage": 1}
        assert cov["needs"]["registry_answered_pct"] == 0 and cov["needs"]["reuse_pct"] == 0

    def test_progress_and_evidence_source(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        cov = q.coverage(conn, project_id=ctx.pid)
        assert cov["progress"]["tasked"] == 1 and cov["progress"]["implemented"] == 0
        assert cov["compliance"]["tasks_worked_without_lock_check"] == 1

        q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": tid, "changed_paths": ["src/bookings/today.py"]})
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        q.record_build_progress(conn, build_task_id=tid, status="verified", verification={
            **PASS, "req_id": "todays-bookings", "criterion_ordinal": 1})
        cov = q.coverage(conn, project_id=ctx.pid)
        assert cov["compliance"]["tasks_worked_without_lock_check"] == 0
        assert cov["progress"]["verified"] == 1 and cov["progress"]["verified_pct"] == 50
        assert cov["progress"]["verified_agent_reported_only"] == 1
        row = next(r for r in cov["by_requirement"] if r["slug"] == "todays-bookings")
        assert row["verified_by"] == "agent_reported" and row["task_status"] == "verified"

    def test_a_stale_lock_does_not_count_as_locked(self, conn):
        ctx = locked_build(conn)
        record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[1], "text": "Changed after the lock"}])
        cov = q.coverage(conn, project_id=ctx.pid)
        assert cov["progress"]["locked"] == 0 and cov["compliance"]["lock_usable"] is False


class TestStatus:

    def test_phases_follow_the_workflow(self, conn):
        def phase(pid):
            return q.pdr_status(conn, project_id=pid)["phase"]

        ing = ingest(conn)
        pid, sid = ing["project_id"], ing["requirement_source_id"]
        assert phase(pid) == "requirements_draft"
        record_reqs(conn, pid, sid)
        st = q.pdr_status(conn, project_id=pid)
        assert st["phase"] == "requirements_draft" and st["requirements"]["blockers"]
        need = record_need(conn, pid)["need_id"]
        rc = q.request_requirements_confirmation(conn, project_id=pid)
        st = q.pdr_status(conn, project_id=pid)
        assert st["phase"] == "awaiting_requirements_confirmation"
        assert st["pending_approvals"][0]["kind"] == "requirements_confirmation"
        approve(conn, rc["approval_request_id"])
        assert phase(pid) == "search_and_decide"
        q.search_for_requirement(conn, req_id=need)
        q.record_decision(conn, req_id=need)
        assert phase(pid) == "ready_to_lock"
        first = q.lock_architecture(conn, project_id=pid)
        assert phase(pid) == "awaiting_lock_approval"
        approve(conn, first["approval_request_id"])
        q.lock_architecture(conn, project_id=pid)
        assert phase(pid) == "building"
        record_reqs(conn, pid, sid, [{**REQS[1], "text": "Changed after the lock"}])
        assert phase(pid) == "requirements_draft"       # scope needs confirming again

    def test_a_fresh_session_can_resume_by_name(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        st = q.pdr_status(conn, project_name="test-project")
        assert st["project_id"] == ctx.pid
        assert st["lock"]["version"] == 1 and st["lock"]["usable"] is True
        assert st["needs"][0] == {
            "need_id": ctx.need_id, "slug": "oauth-login", "satisfies": ["staff-sign-in"],
            "swept": True, "decided": True, "verdict": "BUILD",
            "decided_by": "cip_rules", "in_lock": True}
        assert st["tasks"][0]["build_task_id"] == tid
        criteria = next(r for r in st["requirements"]["items"]
                        if r["slug"] == "staff-sign-in")["criteria"]
        assert [c["ordinal"] for c in criteria] == [1, 2] and criteria[0]["criterion_id"]
        assert q.pdr_status(conn, project_name="nope") is None

    def test_reports_decisions_that_drifted_from_the_lock(self, conn):
        ctx = locked_adopt(conn)
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="changed plan")
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["lock"]["drifted_needs"] == ["oauth-login"]
        assert st["needs"][0]["in_lock"] is False
        assert "relock" in st["next_actions"][0]

    def test_complete_when_everything_in_scope_is_verified(self, conn):
        ctx = locked_build(conn)
        t1 = bespoke_task(conn, ctx)["build_task_id"]
        t2 = bespoke_task(conn, ctx, req_ids=["staff-sign-in", "oauth-login"],
                          permitted_paths=["src/auth/"])["build_task_id"]
        for tid, req, ordinals in ((t1, "todays-bookings", (1,)), (t2, "staff-sign-in", (1, 2))):
            q.record_build_progress(conn, build_task_id=tid, status="in_progress")
            q.record_build_progress(conn, build_task_id=tid, status="implemented")
            r = q.record_build_progress(conn, build_task_id=tid, status="verified", verification=[
                {**PASS, "req_id": req, "criterion_ordinal": o} for o in ordinals])
            assert r["status"] == "verified", r
        assert q.pdr_status(conn, project_id=ctx.pid)["phase"] == "complete"


# ---------------------------------------------------------------------------
# End to end, and the audit trail
# ---------------------------------------------------------------------------

class TestEndToEnd:

    def test_pdr_to_verified_with_a_reused_capability(self, conn):
        _, cv = make_capability(conn, license="MIT")
        ing = ingest(conn, name="e2e")
        pid, sid = ing["project_id"], ing["requirement_source_id"]
        record_reqs(conn, pid, sid)
        need = record_need(conn, pid, constraints=[LICENSE_CONSTRAINT])["need_id"]
        confirm(conn, pid)

        search = q.search_for_requirement(conn, req_id=need)
        assert search["candidate_count"] == 1
        decision = q.record_decision(conn, req_id=need)
        assert (decision["verdict"], decision["decided_by"]) == ("ADOPT", "cip_rules")

        locked = lock(conn, pid, **LOCK_INPUTS)
        assert locked["version"] == 1 and locked["build_authorizations"] == 0

        task = q.record_build_progress(
            conn, project_id=pid, title="Sign-in", objective="Integrate the OAuth library",
            req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"],
            reused_capability_version_id=cv,
        )["build_task_id"]
        check = q.check_against_lock(conn, project_id=pid, proposed_change={
            "build_task_id": task, "changed_paths": ["src/auth/login.py"],
            "dependency_adds": [{"ecosystem": "pypi", "name": "oauth-library"}],
            "provider_changes": [{"name": "Google", "action": "use"}]})
        assert check["verdict"] == "allowed"

        q.record_build_progress(conn, build_task_id=task, status="in_progress")
        q.record_build_progress(conn, build_task_id=task, status="implemented",
                                target_commit="abc1234def")
        done = q.record_build_progress(conn, build_task_id=task, status="verified", verification=[
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1},
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 2},
        ])
        assert done["status"] == "verified"

        cov = q.coverage(conn, project_id=pid)
        assert cov["needs"]["reuse_pct"] == 100 and cov["needs"]["registry_answered_pct"] == 100
        assert cov["progress"]["verified"] == 1 and cov["progress"]["in_scope"] == 2

        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT event_type FROM audit_event WHERE project_id = %s",
                        (pid,))
            events = {r[0] for r in cur.fetchall()}
        assert {
            "pdr_ingested", "requirements_recorded", "capability_needs_recorded",
            "requirements_confirmation_requested", "approval_approved",
            "decision_recorded", "lock_approval_requested", "architecture_locked",
            "task_created", "lock_check", "task_updated", "verification_recorded",
        } <= events

    def test_audit_rows_are_append_only(self, conn):
        ctx = scoped(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE audit_event SET event_type = 'rewritten'")
        conn.commit()
        assert scalar(conn, "SELECT count(*) FROM audit_event "
                            "WHERE event_type = 'rewritten'") == 0
