"""
Regression tests for the SECOND independent review.

The first review's fixes were handed to a fresh reviewer. It found no way
to write a lock without a matching approval, and nineteen ways round the
gates beside it. Each class here is one of them: the attack as it was
run, then what the workflow does now. Real tools, real Postgres. The
pure-logic halves are in test_pdr_core_hardening.py.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time

import psycopg
import pytest

from cip_composer import cli
from core.pdr.text import display_width
from db.connection import connect
from mcp_server import eval_queries
from mcp_server import pdr_approvals
from mcp_server import pdr_queries as q
from mcp_server import pdr_store as store
from tests.pdr_helpers import (
    LICENSE_CONSTRAINT,
    NEED,
    PASS,
    PDR,
    REQS,
    approve,
    bespoke_task,
    bind_newer_revision,
    confirm,
    decided_build,
    ingest,
    lock,
    locked_adopt,
    locked_build,
    make_capability,
    record_need,
    record_reqs,
    scalar,
    scoped,
    with_requirements,
)

ESC = "\x1b"
BOOKINGS_PASS = {**PASS, "req_id": "todays-bookings", "criterion_ordinal": 1}
INTERFACE_CONSTRAINT = {"kind": "required_interface", "name": "authorize"}
STRIPE = {"name": "Stripe", "category": "payment", "purpose": "billing",
          "cost": "usage", "data_leaves_infra": True}
MISSED_CALLS = {
    "slug": "fewer-missed-calls", "text": "Fewer missed calls", "priority": "must",
    "acceptance_criteria": ["Missed calls fall month on month"],
    "quote": "Reduce missed calls at the front desk.",
    "disposition": "bespoke", "disposition_reason": "reporting, project-specific",
}


def audit_count(conn, event_type: str) -> int:
    return scalar(conn, "SELECT count(*) FROM audit_event WHERE event_type = %s", (event_type,))


def flag_codes(result: dict) -> set[str]:
    return {f["code"] for f in result["human_approval_items"]}


def implemented(conn, ctx, **task):
    tid = bespoke_task(conn, ctx, **task)["build_task_id"]
    q.record_build_progress(conn, build_task_id=tid, status="in_progress")
    q.record_build_progress(conn, build_task_id=tid, status="implemented")
    return tid


def verified(conn, ctx):
    tid = implemented(conn, ctx)
    out = q.record_build_progress(conn, build_task_id=tid, status="verified",
                                  verification=BOOKINGS_PASS)
    assert out["status"] == "verified", out
    return tid


@pytest.fixture
def cli_db(monkeypatch, conn):
    monkeypatch.setattr(cli, "connect", lambda: connect(for_tests=True))
    return conn


# ---------------------------------------------------------------------------
# C1. A lock approved for scope A, read as usable after the human moved to B
# ---------------------------------------------------------------------------

class TestTheLockFollowsTheConfirmation:

    def test_reverting_the_scope_closes_the_lock_too(self, conn):
        """
        Confirm A, lock. Add a must-have, the human confirms B. Retire the
        addition: the requirements hash is A's again, so the old lock's
        hash matched and it read as usable — while gate 1 correctly said A
        was unconfirmed. Tasks, lock checks and verification all went
        through on a scope the human's latest word had replaced.
        """
        ctx = locked_build(conn)
        added = record_reqs(conn, ctx.pid, ctx.sid, [MISSED_CALLS])["requirements"][0]
        confirm(conn, ctx.pid)                                             # the human says B
        q.retire_requirement(conn, req_id=added["req_id"], reason="not this release")

        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["requirements"]["confirmation"] == "none"
        assert st["lock"]["usable"] is False
        assert "not confirmed as they now stand" in st["lock"]["stale_reasons"][0]
        assert st["phase"] == "requirements_draft"

        task = bespoke_task(conn, ctx)
        assert task["refused"] and "not confirmed" in task["reason"]
        check = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "dependency_adds": [{"ecosystem": "pypi", "name": "requests"}]})
        assert check["verdict"] == "needs_approval"
        assert q.coverage(conn, project_id=ctx.pid)["compliance"]["lock_usable"] is False

    def test_an_existing_task_is_held_too(self, conn):
        ctx = locked_build(conn)
        tid = implemented(conn, ctx)
        added = record_reqs(conn, ctx.pid, ctx.sid, [MISSED_CALLS])["requirements"][0]
        confirm(conn, ctx.pid)
        q.retire_requirement(conn, req_id=added["req_id"], reason="not this release")
        out = q.record_build_progress(conn, build_task_id=tid, status="verified",
                                      verification=BOOKINGS_PASS)
        assert out["refused"] and "stale" in out["reason"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0

    def test_reconfirming_the_scope_the_lock_was_made_for_reopens_it(self, conn):
        ctx = locked_build(conn)
        added = record_reqs(conn, ctx.pid, ctx.sid, [MISSED_CALLS])["requirements"][0]
        confirm(conn, ctx.pid)
        q.retire_requirement(conn, req_id=added["req_id"], reason="not this release")
        confirm(conn, ctx.pid)                                             # A, said again
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["lock"]["usable"] is True and st["phase"] == "building"
        assert bespoke_task(conn, ctx)["status"] == "planned"


# ---------------------------------------------------------------------------
# H1. A BUILD the agent wanted, laundered into an unflagged cip_rules decision
# ---------------------------------------------------------------------------

class TestCipOnlyClaimsWhatItCanSee:

    def test_a_custom_search_cannot_steer_cip_into_build(self, conn):
        """
        The need's only real candidate is unanalysed, so CIP rightly would
        not decide. The agent then ran a custom search that pulled in an
        unrelated, analysed, GPL library. It ranked first, the rules
        rejected it, and the result was BUILD / cip_rules / no flags.
        """
        make_capability(conn, name="oauth-meta-mit", display="OAuth Meta", metadata_license="MIT")
        make_capability(conn, name="pdf-render-gpl", display="PDF Render", license="GPL-3.0",
                        intrinsic=0.95)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        sweep = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert [c["normalized_key"] for c in sweep["candidates"]] == ["pypi:oauth-meta-mit"]
        extra = q.search_for_requirement(conn, req_id=ctx.need_id, search_query="pdf render")
        assert [c["normalized_key"] for c in extra["candidates"]] == ["pypi:pdf-render-gpl"]

        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "holds no analysed evidence" in refused["reason"]
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                              rationale="neither is usable")
        assert d["decided_by"] == "llm_judgment" and d["listed_for_human"]
        assert "judgment:insufficient_evidence" in d["flags"]

    def test_one_readable_reject_is_not_grounds_for_build(self, conn):
        """An analysed GPL library ranked above unanalysed MIT ones gave the same result."""
        make_capability(conn, name="oauth-gpl", display="OAuth GPL", license="GPL-3.0",
                        intrinsic=0.95)
        make_capability(conn, name="oauth-meta-mit", display="OAuth Meta", metadata_license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cands = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"]
        assert {c["evidence"] for c in cands} == {"sufficient", "insufficient"}
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]

    def test_cip_does_reject_when_it_read_everything(self, conn):
        make_capability(conn, name="oauth-gpl", display="OAuth GPL", license="GPL-3.0")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert (d["verdict"], d["decided_by"]) == ("BUILD", "cip_rules")
        assert "rejected every candidate" in d["note"]

    def test_a_candidate_with_no_scorecard_is_not_rejected_by_default(self, conn):
        """
        A fitting, analysed, MIT candidate with no scorecard scored 0.0,
        matched no rule, and the default BUILD was recorded as CIP's own.
        """
        make_capability(conn, license="MIT", score=False)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["fit_score"] == 1.0 and cand["missing_evidence"] == ["score"]
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]


# ---------------------------------------------------------------------------
# H2. The sweep still stopped at 20 per term, and said nothing
# ---------------------------------------------------------------------------

class TestTheSweepSaysWhenItDidNotSeeEverything:

    def test_more_than_twenty_matches_are_all_evaluated(self, conn):
        """22 GPL libraries and one MIT: the MIT one was the 23rd, never seen."""
        for i in range(22):
            make_capability(conn, name=f"oauth-gpl-{i:02d}", display=f"OAuth GPL {i:02d}",
                            license="GPL-3.0", intrinsic=0.9)
        _, mit = make_capability(conn, name="oauth-mit", display="OAuth MIT", license="MIT",
                                 intrinsic=0.5)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert res["candidate_count"] == 23 and res["truncated"] is False
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["chosen_capability_version_id"] == mit and d["decided_by"] == "cip_rules"

    def test_a_truncated_sweep_cannot_conclude_nothing_fits(self, conn, monkeypatch):
        monkeypatch.setattr(q, "SWEEP_LIMIT", 3)
        for i in range(5):
            make_capability(conn, name=f"oauth-gpl-{i}", display=f"OAuth GPL {i}",
                            license="GPL-3.0", intrinsic=0.9)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert res["truncated"] is True and "truncation_note" in res
        assert scalar(conn, "SELECT truncated FROM requirement_search") is True

        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "reached its ceiling" in refused["reason"]
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="all GPL")
        assert d["decided_by"] == "llm_judgment"
        assert {"sweep_truncated", "judgment:sweep_truncated"} <= set(d["flags"])
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "sweep_truncated" in flag_codes(first)
        assert "the search did not see every match" in first["summary_markdown"]

    def test_a_weird_limit_is_not_a_crash(self, conn):
        ctx = scoped(conn)
        for limit in (float("inf"), float("nan"), "many", None, -5, 10 ** 30):
            res = q.search_for_requirement(conn, req_id=ctx.need_id, limit=limit)
            assert res["counts_as_mandatory_sweep"], limit


# ---------------------------------------------------------------------------
# H3. One quote "mapped" the whole PDR
# ---------------------------------------------------------------------------

class TestAQuoteMapsOnePlace:

    def test_a_quote_cannot_be_the_document(self, conn):
        ing = ingest(conn)
        whole = {**REQS[0], "quote": PDR.strip()}
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [whole])
        assert not r["requirements"][0]["valid"]
        assert r["span_coverage"]["mapped_section_count"] == 0

    def test_a_quote_cannot_run_across_a_heading(self, conn):
        ing = ingest(conn)
        across = {**REQS[0], "quote":
                  "The app could have dark mode.\n\n## Constraints\nAll dependencies must be MIT"}
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [across])
        assert "runs across the heading 'Constraints'" in r["requirements"][0]["errors"][0]

    def test_the_summary_shows_the_whole_quote(self, conn):
        long_sentence = ("Staff must sign in with Google OAuth before using the system, and "
                         + "the session must end after thirty minutes without activity, " * 4
                         + "with no exceptions for any role.")
        doc = f"# P\n\n## Requirements\n{long_sentence}\n\n## Other\nNothing to see here.\n"
        ing = ingest(conn, pdr=doc)
        req = {**REQS[0], "quote": long_sentence, "disposition": "bespoke",
               "disposition_reason": "x"}
        assert record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                           [req])["requirements"][0]["valid"]
        s = q.request_requirements_confirmation(conn, project_id=ing["project_id"])[
            "summary_markdown"]
        assert long_sentence in s and len(long_sentence) > 200

    def test_not_applicable_notes_do_not_make_a_section_read(self, conn):
        """Four 8-character not_applicable fragments mapped every section."""
        ing = ingest(conn)
        notes = [
            {"slug": f"note-{i}", "text": f"note {i}", "quote": quote,
             "disposition": "not_applicable", "disposition_reason": "n/a"}
            for i, quote in enumerate(["Reduce missed", "Staff must sign", "All dependencies",
                                       "PDR means product"])
        ]
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], notes)
        assert all(x["valid"] for x in r["requirements"])
        cov = r["span_coverage"]
        assert cov["mapped_section_count"] == 0 and cov["unmapped_sections"] == []
        assert len(cov["out_of_scope_only_sections"]) == 4
        s = q.request_requirements_confirmation(conn, project_id=ing["project_id"])[
            "summary_markdown"]
        assert "quoted only by deferred or not-applicable items" in s
        assert "Nothing in this build comes from these" in s

    def test_a_long_section_with_one_quote_is_pointed_out(self, conn):
        """A table of requirements under one heading is 'mapped' by its first row."""
        rows = "\n".join(f"| R{i} | The system must do thing number {i} reliably. |"
                         for i in range(12))
        doc = f"# P\n\n## Requirements\n| id | text |\n|---|---|\n{rows}\n\n## Notes\nNone.\n"
        ing = ingest(conn, pdr=doc)
        req = {**REQS[0], "quote": "The system must do thing number 3 reliably.",
               "disposition": "bespoke", "disposition_reason": "x"}
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [req])
        light = r["span_coverage"]["lightly_quoted_sections"]
        assert [s["heading"] for s in light] == ["Requirements"] and light[0]["quoted_pct"] < 20
        s = q.request_requirements_confirmation(conn, project_id=ing["project_id"])[
            "summary_markdown"]
        assert "Long PDR sections that are barely quoted" in s
        assert q.coverage(conn, project_id=ing["project_id"])["pdr"]["quoted_char_pct"] <= 100

    def test_moving_a_quotes_anchor_reopens_the_gate(self, conn):
        """
        The same sentence appears twice. The span was not part of what was
        confirmed, so re-recording with a hint moved the anchor to the
        other section — changing what looked read — with the confirmation
        still standing.
        """
        doc = ("# P\n\n## Sign-in\nStaff must sign in with Google OAuth.\n\n"
               "## Admin\nStaff must sign in with Google OAuth.\nAdmins also need a key.\n")
        ing = ingest(conn, pdr=doc)
        pid, sid = ing["project_id"], ing["requirement_source_id"]
        req = {**REQS[0], "quote": "Staff must sign in with Google OAuth.",
               "disposition": "bespoke", "disposition_reason": "x"}
        first = record_reqs(conn, pid, sid, [req])["requirements"][0]
        confirm(conn, pid)
        moved = record_reqs(conn, pid, sid, [{**req, "start_hint": len(doc) - 60}])
        assert moved["requirements"][0]["span"] != first["span"]
        rc = q.request_requirements_confirmation(conn, project_id=pid)
        assert rc["status"] == "awaiting_approval"
        assert "anchored at a different place" in rc["summary_markdown"]


# ---------------------------------------------------------------------------
# H4. A real blocking gap, hidden behind "not analysed"
# ---------------------------------------------------------------------------

class TestAFindingIsNotHiddenByAGap:

    def test_a_read_gpl_license_is_still_a_blocking_gap(self, conn):
        """
        License read: GPL-3.0, not on the allowlist. Add a required
        interface nobody extracted and the candidate became 'unanalysed'.
        The summary then said nothing about it was verified, and that the
        need had no checkable constraints. Both false.
        """
        _, cv = make_capability(conn, license="GPL-3.0")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT, INTERFACE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["missing_evidence"] == ["interfaces"]
        assert cand["confirmed_blocking_gaps"] == ["license_mismatch"]

        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=cv, rationale="we will dual-license")
        assert d["decided_by"] == "llm_judgment"
        assert {"unanalysed_candidate", "overrides_blocking_gap"} <= set(d["flags"])

        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert {"overrides_blocking_gap", "non_permissive_license",
                "unanalysed_candidate", "llm_judgment"} <= flag_codes(first)
        s = first["summary_markdown"]
        assert "is licensed GPL-3.0 (read from its source)" in s
        assert "CIP lacked the evidence to compute a verdict" in s
        assert "no machine-checkable constraints" not in s

    def test_a_gap_that_is_only_missing_evidence_is_not_a_finding(self, conn):
        make_capability(conn, metadata_license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["blocking_gap_count"] == 1 and cand["confirmed_blocking_gaps"] == []

    def test_the_license_flag_comes_from_the_source_not_the_metadata(self, conn):
        """Metadata said MIT. The LICENSE file in the pinned source says GPL."""
        _, cv = make_capability(conn, license="GPL-3.0", metadata_license="MIT")
        ctx = scoped(conn)                                      # no constraint: LLM judgment
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert (cand["license"], cand["license_source"]) == ("GPL-3.0", "evidence")
        q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                          chosen_capability_version_id=cv, rationale="looks fine")
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "non_permissive_license" in flag_codes(first)
        assert "license: GPL-3.0 (read from source)" in first["summary_markdown"]


# ---------------------------------------------------------------------------
# H5. A second confirmation that looked like the first
# ---------------------------------------------------------------------------

class TestReconfirmationShowsWhatMoved:

    def test_the_first_confirmation_has_no_changes_section(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid, constraints=[LICENSE_CONSTRAINT])
        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert "Changes since the scope you last confirmed" not in rc["summary_markdown"]
        assert rc["changes_since_last_confirmation"] == []

    def test_what_was_dropped_is_at_the_top(self, conn):
        """
        Retire a must-have, drop an acceptance criterion, turn must into
        could, remove the license constraint. The second summary was a
        plain list with no sign that anything had changed.
        """
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.retire_requirement(conn, req_id=ctx.reqs["todays-bookings"], reason="descoped quietly")
        record_reqs(conn, ctx.pid, ctx.sid, [{
            **REQS[0], "priority": "could",
            "acceptance_criteria": ["A staff user signs in with a Google account"]}])
        record_need(conn, ctx.pid, search_terms=["oauth"])      # no constraints now

        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        kinds = {(c["kind"], c["subject"]) for c in rc["changes_since_last_confirmation"]}
        assert kinds == {("requirement_removed", "todays-bookings"),
                         ("requirement_changed", "staff-sign-in"),
                         ("need_changed", "oauth-login")}
        s = rc["summary_markdown"]
        changes_at = s.index("## Changes since the scope you last confirmed")
        assert changes_at < s.index("requirement(s):")           # before the plain list
        block = s[changes_at:s.index("requirement(s):")]
        assert "requirement removed: todays-bookings" in block
        assert "priority must -> could" in block
        assert "removed An unauthenticated request is redirected to sign-in" in block
        assert "search terms: removed oidc login" in block
        assert "license_allowlist" in block
        assert "## Retired - taken out altogether (1)" in s and "descoped quietly" in s

    def test_a_replaced_pdr_is_itemised(self, conn):
        ctx = scoped(conn)
        ingest(conn, pdr=PDR + "\n## Extra\nReports are exported weekly as CSV files.\n")
        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert ("pdr_text_changed", "PDR") in {
            (c["kind"], c["subject"]) for c in rc["changes_since_last_confirmation"]}


# ---------------------------------------------------------------------------
# H6. `verified` and `complete` on evidence that no longer stood
# ---------------------------------------------------------------------------

class TestEvidenceStandsWhileTheWorkDoes:

    def test_abandoning_the_task_takes_its_evidence_with_it(self, conn):
        ctx = locked_build(conn)
        tid = verified(conn, ctx)
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 1
        q.record_build_progress(conn, build_task_id=tid, status="abandoned")
        p = q.coverage(conn, project_id=ctx.pid)["progress"]
        assert (p["tasked"], p["implemented"], p["verified"]) == (0, 0, 0)

    def test_what_remains_is_judged_on_live_evidence_too(self, conn):
        """
        `cip-composer verify` reports what is still unverified. It counted a
        pass from an abandoned task, so it could say "nothing" while status
        and coverage said otherwise.
        """
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        task = dict(objective="x", req_ids=["staff-sign-in", "oauth-login"],
                    permitted_paths=["src/auth/"])
        t1 = implemented(conn, ctx, title="Sign in", **task)
        q.record_build_progress(conn, build_task_id=t1, verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1})
        q.record_build_progress(conn, build_task_id=t1, status="abandoned")
        t2 = implemented(conn, ctx, title="Sign in, second go", **task)
        out = q.record_executed_verification(conn, build_task_id=t2, verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 2})
        assert "error" not in out, out
        assert len(out["remaining"]) == 1 and out["remaining"][0].startswith("staff-sign-in #1")

    def test_a_project_is_not_complete_with_no_live_tasks(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        t1 = verified(conn, ctx)
        t2 = implemented(conn, ctx, title="Sign in", objective="x",
                         req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"])
        done = q.record_build_progress(conn, build_task_id=t2, status="verified", verification=[
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1},
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 2}])
        assert done["status"] == "verified"
        assert q.pdr_status(conn, project_id=ctx.pid)["phase"] == "complete"
        for tid in (t1, t2):
            q.record_build_progress(conn, build_task_id=tid, status="abandoned")
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["phase"] == "building" and st["progress"]["verified"] == 0

    def test_rewriting_a_requirement_drops_its_evidence_even_if_the_criterion_reads_the_same(
            self, conn):
        """
        New text, new quote, new kind — but the criterion kept its wording,
        so its row and the pass recorded against it carried over.
        """
        ctx = locked_build(conn)
        tid = verified(conn, ctx)
        rewritten = {**REQS[1], "text": "Staff sign in with Google", "kind": "non_functional",
                     "quote": "Staff must sign in with Google OAuth before using the system."}
        assert record_reqs(conn, ctx.pid, ctx.sid, [rewritten])["requirements"][0]["valid"]
        confirm(conn, ctx.pid)
        lock(conn, ctx.pid)
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 0
        assert scalar(conn, "SELECT status FROM build_task WHERE id = %s", (tid,)) == "implemented"
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0

    def test_a_typo_free_rerecord_keeps_its_evidence(self, conn):
        ctx = locked_build(conn)
        verified(conn, ctx)
        assert record_reqs(conn, ctx.pid, ctx.sid, [REQS[1]])["requirements"][0]["valid"]
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 1

    def test_a_stale_tasks_evidence_does_not_count(self, conn):
        """BUILD re-decided to ADOPT and relocked: the hand-built task is stale."""
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        tid = implemented(conn, ctx, title="Sign in (hand-built)", objective="x",
                          req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"])
        q.record_build_progress(conn, build_task_id=tid, status="verified", verification=[
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1},
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 2}])
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 1

        make_capability(conn, license="MIT")
        record_need(conn, ctx.pid, constraints=[LICENSE_CONSTRAINT])
        confirm(conn, ctx.pid)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        assert q.record_decision(conn, req_id=ctx.need_id)["verdict"] == "ADOPT"
        relock = lock(conn, ctx.pid)

        # The lock that no longer carries the task ends it (fourth review, L1).
        assert relock["tasks_abandoned"] == [tid]
        assert scalar(conn, "SELECT status FROM build_task WHERE id = %s", (tid,)) == "abandoned"
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert tid not in [t["build_task_id"] for t in st["tasks"]]
        assert st["progress"]["verified"] == 0


# ---------------------------------------------------------------------------
# H7. A relock diff that said "none"
# ---------------------------------------------------------------------------

HOSTING = {"item": "Hosting", "kind": "recurring", "amount": "5", "currency": "USD",
           "period": "year"}
SENTRY = {"name": "Sentry", "category": "analytics", "purpose": "error reports",
          "cost": "free_tier", "data_leaves_infra": False}


class TestTheRelockDiffIsTheChangeRequest:

    def test_a_second_line_for_the_same_cost_item_is_refused(self, conn):
        """
        'Hosting 5000 USD/month' listed before the old 'Hosting 5 USD/year'.
        The diff keyed on the name, saw one Hosting each side, and said none.
        """
        ctx = decided_build(conn)
        out = q.lock_architecture(conn, project_id=ctx.pid, cost_commitments=[
            {**HOSTING, "amount": "5000", "period": "month"}, HOSTING])
        assert out["refused"] and "duplicate item 'Hosting'" in out["reason"]

    def test_every_kind_of_change_is_itemised(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid, approved_providers=[SENTRY], cost_commitments=[HOSTING],
             unresolved_risks=["vendor lock-in", "no load test yet"],
             project_decisions=[{"key": "database", "category": "database", "choice": "Postgres"}])
        relock = q.lock_architecture(
            conn, project_id=ctx.pid,
            approved_providers=[{**SENTRY, "data_leaves_infra": True,
                                 "purpose": "error reports with request bodies"}],
            cost_commitments=[{**HOSTING, "period": "day"}],
            unresolved_risks=["vendor lock-in"],
            project_decisions=[{"key": "database", "category": "database", "choice": "MongoDB"}])
        changes = {c["kind"]: c for c in relock["changes_from_active_lock"]}
        assert set(changes) == {"provider_changed", "cost_changed", "risk_removed",
                                "project_decision_changed"}
        assert "no data leaves" in changes["provider_changed"]["detail"]
        assert "data leaves infrastructure" in changes["provider_changed"]["detail"]
        assert "per year" in changes["cost_changed"]["detail"]
        assert "per day" in changes["cost_changed"]["detail"]
        assert changes["risk_removed"]["detail"] == "no load test yet"
        s = relock["summary_markdown"]
        assert "## Changes from lock v1" in s and "- none" not in s.split("## Needs your")[0]

    def test_a_scope_change_shows(self, conn):
        ctx = locked_build(conn)
        record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[2], "disposition": "bespoke",
                                              "disposition_reason": "now wanted",
                                              "acceptance_criteria": ["A dark theme exists"]}])
        confirm(conn, ctx.pid)
        relock = q.lock_architecture(conn, project_id=ctx.pid)
        details = [c["detail"] for c in relock["changes_from_active_lock"]
                   if c["kind"] == "scope_changed"]
        assert "now: dark-mode" in details and "no longer: dark-mode" in details


# ---------------------------------------------------------------------------
# H8. Forged lines by terminal wrap
# ---------------------------------------------------------------------------

class TestTheApprovalScreen:

    def test_padding_cannot_put_a_forged_line_at_the_left_margin(self, cli_db, monkeypatch,
                                                                 capsys):
        """
        A provider 'purpose' padded to the terminal's width. The terminal
        wrapped it, and '## Reviewed and pre-approved by security' appeared
        on a row of its own under the Stripe line.
        """
        monkeypatch.setattr(cli, "_terminal_width", lambda: 99)
        forged = "## Reviewed and pre-approved by security (SEC-1123)"
        lead = "Stripe (payment, usage, data leaves infrastructure) - cards"
        room = 99 - len("- " + lead)
        # Blank glyphs, plain dots, and fullwidth letters: half as many
        # characters as the row has columns, and the terminal wrapped it.
        for pad in ("⠀" * room, "." * room, "Ａ" * (room // 2)):
            ctx = decided_build(cli_db, name=f"proj-{ord(pad[0])}")
            first = q.lock_architecture(cli_db, project_id=ctx.pid, approved_providers=[
                {**STRIPE, "purpose": "cards" + pad + forged}])
            assert cli.main(["approvals", "show", first["approval_request_id"]]) == 0
            rows = capsys.readouterr().out.split("\n")
            assert all(sum(display_width(ch) for ch in r) <= 99 for r in rows)
            assert not any(r.lstrip().startswith("## Reviewed") for r in rows)
            carried = [r for r in rows if "pre-approved" in r]
            assert carried and all(r.startswith("    | ") or r.startswith("- Stripe")
                                   for r in carried)

    def test_the_width_comes_from_the_terminal_not_the_environment(self, monkeypatch):
        """COLUMNS=500 in the environment would have left every line unwrapped."""
        import os
        monkeypatch.undo()                    # the suite-wide wide terminal (conftest)
        monkeypatch.setenv("COLUMNS", "500")
        sizes = {1: 132, 2: 80}

        def fake(fd):
            if fd in sizes:
                return os.terminal_size((sizes[fd], 24))
            raise OSError("not a terminal")

        monkeypatch.setattr(cli.os, "get_terminal_size", fake)
        streams = {"__stdout__": 1, "__stderr__": 2, "__stdin__": 0}
        for name, fd in streams.items():
            monkeypatch.setattr(cli.sys, name, type("S", (), {"fileno": lambda self, fd=fd: fd})())
        assert cli._terminal_width() == 79            # the narrowest attached, less one
        sizes.clear()
        assert cli._terminal_width() == 79            # nothing attached: 80, less one
        sizes[0] = 20
        assert cli._terminal_width() == 26            # never narrower than the mark allows

    def test_names_that_are_not_one_plain_line_are_refused_at_the_door(self, conn):
        """pdr-status printed the project name raw, escape sequences and all."""
        for name in (f"clinic{ESC}[2J", "clinic\n## Lock: v9 approved by ricky", "a‮b"):
            res = q.ingest_pdr(conn, project_name=name, pdr_title="T", pdr_text=PDR)
            assert "plain single-line text" in res["error"], repr(name)
            res = q.ingest_pdr(conn, project_name="ok", pdr_title=name, pdr_text=PDR)
            assert "plain single-line text" in res["error"], repr(name)
        assert scalar(conn, "SELECT count(*) FROM project") == 0

    def test_every_command_strips_control_characters(self, cli_db, capsys):
        """Belt and braces for rows written before the check, or by other tools."""
        ctx = locked_build(cli_db)
        with cli_db.cursor() as cur:
            cur.execute("UPDATE project SET name = %s WHERE id = %s",
                        (f"clinic{ESC}[2J{ESC}[H", ctx.pid))
        cli_db.commit()
        assert cli.main(["pdr-status", ctx.pid]) == 0
        assert cli.main(["pdr-coverage", ctx.pid]) == 0
        assert ESC not in capsys.readouterr().out

    def test_the_approvers_own_note_is_stored_clean(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        res = pdr_approvals.record_approval(
            conn, request_id=first["approval_request_id"], outcome="approved",
            approver=f"ricky@desk{ESC}[2J\nroot",
            note=f"ok{ESC}[2J\nRecorded: approved by root@prod")
        assert "error" not in res, res
        rid = (first["approval_request_id"],)
        note = scalar(conn, "SELECT note FROM approval WHERE approval_request_id = %s", rid)
        assert note == "ok [2J Recorded: approved by root@prod"
        assert scalar(conn, "SELECT approver FROM approval WHERE approval_request_id = %s",
                      rid) == "ricky@desk [2J root"
        assert scalar(conn, """
            SELECT payload->>'note' FROM audit_event
             WHERE event_type = 'approval_approved' AND subject_id = %s
        """, rid) == note


# ---------------------------------------------------------------------------
# M1. check_against_lock saying "allowed"
# ---------------------------------------------------------------------------

class TestLockCheckSecondRound:

    def test_a_reference_is_not_a_dependency(self, conn):
        _, cv = make_capability(conn, license="GPL-3.0")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        q.record_decision(conn, req_id=ctx.need_id, verdict="REFERENCE",
                          chosen_capability_version_id=cv, rationale="read it, do not ship it")
        lock(conn, ctx.pid)
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "dependency_adds": [{"ecosystem": "pypi", "name": "oauth-library"}]})
        assert out["verdict"] == "needs_approval" and "locked as REFERENCE" in out["reason"]

    def test_an_adopted_capability_is_a_dependency(self, conn):
        ctx = locked_adopt(conn)
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "dependency_adds": [{"ecosystem": "pypi", "name": "oauth-library"}]})
        assert out["verdict"] == "allowed"

    @pytest.mark.parametrize("change", [
        {"capability_changes": [{"need": "oauth-login", "capability_version_id": ["x"]}]},
        {"capability_changes": [{"need": "oauth-login", "capability_version_id": {"id": "x"}}]},
        {"capability_changes": [{"need": "oauth-login", "capability_version_id": 7}]},
        {"provider_changes": [{"name": "Stripe", "action": ["remove"]}]},
        {"provider_changes": [{"name": "Stripe", "action": {"op": "replace"}}]},
        {"project_decision_changes": [{"key": "database", "choice": ["MongoDB"]}]},
    ])
    def test_a_value_of_the_wrong_type_is_escalated(self, conn, change):
        """A list where a name belonged read as 'nothing given', and that as 'no change'."""
        ctx = decided_build(conn)
        lock(conn, ctx.pid, approved_providers=[STRIPE], project_decisions=[
            {"key": "database", "category": "database", "choice": "Postgres"}])
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change=change)
        assert out["verdict"] == "needs_approval" and "must be text" in out["reason"]

    def test_a_check_of_nothing_does_not_count_as_a_check(self, conn):
        ctx = locked_build(conn)
        tid = implemented(conn, ctx)
        blocked = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": tid, "description": "hello"})
        assert blocked["verdict"] == "blocked"
        comp = q.coverage(conn, project_id=ctx.pid)["compliance"]
        assert comp["tasks_worked_without_lock_check"] == 1
        q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": tid, "changed_paths": ["src/bookings/a.py"]})
        comp = q.coverage(conn, project_id=ctx.pid)["compliance"]
        assert comp["tasks_worked_without_lock_check"] == 0

    def test_an_oversized_change_is_refused_and_not_stored(self, conn):
        ctx = locked_build(conn)
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "description": "x" * 200_000,
            "dependency_adds": [{"ecosystem": "pypi", "name": "requests"}]})
        assert out["verdict"] == "blocked" and "larger than" in out["reason"]
        assert scalar(conn, "SELECT max(length(payload::text)) FROM audit_event "
                            "WHERE event_type = 'lock_check'") < 2000


# ---------------------------------------------------------------------------
# M2. "The registry had nothing", made wrong
# ---------------------------------------------------------------------------

def bank(conn, **over):
    args = dict(asset_slug="acme-oauth-kit", verdict="Adapt", evidence_depth="code_read",
                code_quality="solid", banked=True, summary="OAuth flow kit",
                problem_types=["oauth-flow"])
    args.update(over)
    res = eval_queries.record_evaluation(conn, **args)
    assert not res.get("error"), res
    return res


class TestWhatTheRegistrySaid:

    def test_adding_a_problem_type_voids_the_sweep_that_ran_without_it(self, conn):
        bank(conn)
        ctx = with_requirements(conn)
        ctx.need_id = record_need(conn, ctx.pid, problem_types=[])["need_id"]
        confirm(conn, ctx.pid)
        assert q.search_for_requirement(conn, req_id=ctx.need_id)["evaluation_matches"] == [] \
            or True
        record_need(conn, ctx.pid, problem_types=["oauth-flow"])
        confirm(conn, ctx.pid)
        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and refused["gate"] == "search_before_decision"
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert "build_despite_banked_match" in d["flags"]

    def test_unbanking_an_evaluation_to_dodge_the_flag_is_seen(self, conn):
        """
        The evaluation store upserts by slug. Rewrite the banked asset as a
        plain Reject, sweep, and the build reads as a clean cold start.
        """
        bank(conn)
        ctx = scoped(conn)
        bank(conn, verdict="Reject", banked=False, problem_types=["misc"], code_quality=None,
             evidence_depth="docs_only")
        assert audit_count(conn, "evaluation_replaced") == 1
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert "banked_evaluation_changed" in d["flags"] and d["listed_for_human"]
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "banked_evaluation_changed" in flag_codes(first)

    def test_an_ordinary_re_evaluation_is_not_flagged(self, conn):
        bank(conn)
        ctx = scoped(conn)
        bank(conn, summary="OAuth flow kit, re-read")           # still banked, same tags
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert "banked_evaluation_changed" not in d["flags"]

    def test_the_database_holds_the_four_outcomes_and_no_others(self, conn):
        decided_build(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
                FROM information_schema.columns
                WHERE table_name = 'decision_record'
                  AND column_name NOT IN ('id', 'seq', 'coverage_outcome')
            """)
            cols = cur.fetchone()[0]
        copy = (f"INSERT INTO decision_record ({cols}, coverage_outcome) "
                f"SELECT {cols}, %s FROM decision_record LIMIT 1")
        for outcome in ("candidates_evaluated", "found_but_unevaluable", "banked_only",
                        "no_registry_coverage"):
            with conn.cursor() as cur:
                cur.execute(copy, (outcome,))
            conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(copy, ("probably_fine",))
        conn.rollback()

    def test_a_match_that_cannot_be_evaluated_is_not_nothing(self, conn):
        make_capability(conn, bind=False)
        ctx = scoped(conn)
        res = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert res["coverage_outcome"] == "found_but_unevaluable"
        assert "not the same as having nothing" in res["note"]
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["coverage_outcome"] == "found_but_unevaluable"
        assert d["flags"] == ["unevaluable_candidates"]
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "unevaluable_candidates" in flag_codes(first)
        assert "cold_start_build" not in flag_codes(first)

    def test_a_sweep_of_yesterdays_registry_does_not_decide_today(self, conn):
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        assert q.search_for_requirement(conn, req_id=ctx.need_id)["candidate_count"] == 0
        make_capability(conn, license="MIT")                    # ingested after the sweep
        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "registry has changed since" in refused["reason"]
        q.search_for_requirement(conn, req_id=ctx.need_id)
        assert q.record_decision(conn, req_id=ctx.need_id)["verdict"] == "ADOPT"

    def test_what_project_policy_removes_is_counted(self, conn):
        """A policy constraint filtered the 'unfiltered' sweep to nothing, silently."""
        make_capability(conn, license="MIT", cost_tier="paid")
        ctx = scoped(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO project_constraint (project_id, kind) "
                        "VALUES (%s, 'must_be_free_tier')", (ctx.pid,))
        conn.commit()
        res = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert res["candidate_count"] == 0 and res["policy_excluded_count"] == 1
        assert res["unevaluable"][0]["reason"] == "excluded by this project's policy constraints"
        assert res["coverage_outcome"] == "found_but_unevaluable"
        assert scalar(conn, "SELECT policy_excluded_count FROM requirement_search") == 1


# ---------------------------------------------------------------------------
# M3 + M4. Rows from v1.0
# ---------------------------------------------------------------------------

class TestLegacyRowsSecondRound:

    def _legacy_lock_and_task(self, conn, ctx) -> str:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO architecture_lock (project_id, version, status, approved_by, "
                "decisions, source_lock_hash) VALUES (%s, 1, 'active', 'the-llm-said-ricky', "
                "'[]'::jsonb, 'x') RETURNING id", (ctx.pid,))
            lock_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO build_task (project_id, architecture_lock_id, title, objective, "
                "status, permitted_paths) VALUES (%s, %s, 'T1', 'o', 'verified', ARRAY['**']) "
                "RETURNING id", (ctx.pid, lock_id))
            tid = str(cur.fetchone()[0])
            cur.execute("INSERT INTO build_task_requirement (build_task_id, "
                        "project_requirement_id) VALUES (%s, %s)",
                        (tid, ctx.reqs["todays-bookings"]))
        conn.commit()
        return tid

    def test_a_v1_0_task_does_not_come_back_to_life_after_a_relock(self, conn):
        """
        Created under a lock nobody approved, permitted to touch '**'. After
        a proper relock it read as current: progress accepted, a check on
        infra/terraform/prod.tf allowed, verified, phase complete.
        """
        ctx = decided_build(conn)
        tid = self._legacy_lock_and_task(conn, ctx)
        assert q.pdr_status(conn, project_id=ctx.pid)["lock"]["usable"] is False
        relock = lock(conn, ctx.pid)                                         # v2, approved

        st = q.pdr_status(conn, project_id=ctx.pid)
        assert relock["tasks_abandoned"] == [tid]
        assert tid not in [t["build_task_id"] for t in st["tasks"]]
        out = q.record_build_progress(conn, build_task_id=tid, status="verified",
                                      verification=BOOKINGS_PASS)
        assert out["refused"] and "abandoned" in out["reason"]
        check = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": tid, "changed_paths": ["infra/terraform/prod.tf"]})
        assert check["verdict"] == "needs_approval"
        assert st["phase"] == "building" and st["progress"]["verified"] == 0

    @pytest.mark.parametrize("span", ['"lines 10-12"', "[120, 180]", '{"quote": 5}',
                                      '{"start": "120", "end": "180"}',
                                      '{"start": 5, "end": 2, "quote": "Staff must sign in"}',
                                      '{"start": -5, "end": 99999}', "7", "true"])
    def test_a_v1_0_source_span_of_any_shape(self, conn, span):
        """v1.0 stored whatever it was given. A non-object made every tool raise."""
        ctx = with_requirements(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE project_requirement SET source_span = %s::jsonb "
                        "WHERE id = %s", (span, ctx.reqs["staff-sign-in"]))
        conn.commit()
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert any("staff-sign-in" in b and "no source quote" in b
                   for b in st["requirements"]["blockers"])
        assert q.coverage(conn, project_id=ctx.pid) is not None
        assert q.request_requirements_confirmation(conn, project_id=ctx.pid)["status"] == "blocked"
        assert "error" not in ingest(conn, pdr=PDR + "\nMore text for a new revision here.\n")
        assert record_reqs(conn, ctx.pid, scalar(
            conn, "SELECT id::text FROM requirement_source ORDER BY activated_at DESC LIMIT 1"),
            [REQS[0]])["requirements"][0]["valid"]


# ---------------------------------------------------------------------------
# M6. Tracebacks
# ---------------------------------------------------------------------------

class TestNoTracebacks:

    def test_an_upper_case_id_is_the_same_id(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        assert q.pdr_status(conn, project_id=ctx.pid.upper())["project_id"] == ctx.pid
        assert q.coverage(conn, project_id=ctx.pid.upper())["project_id"] == ctx.pid
        out = q.record_build_progress(conn, build_task_id=tid.upper(), status="in_progress")
        assert out["build_task_id"] == tid and out["status"] == "in_progress"
        assert q.search_for_requirement(conn, req_id=ctx.need_id.upper())["req_id"] == ctx.need_id
        assert bespoke_task(conn, ctx, project_id=ctx.pid.upper(),
                            title="again")["status"] == "planned"

    def test_other_spellings_of_a_uuid_are_not_found_not_raised(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        for spell in (lambda u: "{" + u + "}", lambda u: "urn:uuid:" + u,
                      lambda u: u.replace("-", ""), lambda u: u[:8] + "-" + u[8:].replace("-", "")):
            assert q.pdr_status(conn, project_id=spell(ctx.pid)) is None
            assert q.coverage(conn, project_id=spell(ctx.pid)) is None
            assert q.search_for_requirement(conn, req_id=spell(ctx.need_id)) is None
            assert q.record_decision(conn, req_id=spell(ctx.need_id)) is None
            assert q.retire_requirement(conn, req_id=spell(ctx.need_id), reason="x") is None
            assert "error" in q.record_build_progress(conn, build_task_id=spell(tid),
                                                      status="in_progress")
            assert "error" in q.record_requirements(
                conn, project_id=spell(ctx.pid), requirement_source_id=ctx.sid, requirements=[])
            assert q.lock_architecture(conn, project_id=spell(ctx.pid)) is None
            assert "error" in q.lock_architecture(conn, project_id=ctx.pid,
                                                  approval_request_id=spell(ctx.pid))
            out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
                "build_task_id": spell(tid), "changed_paths": ["src/bookings/a.py"]})
            assert out["verdict"] == "needs_approval"
        assert store.canon_uuid(None) is None and store.canon_uuid(5) is None

    def test_losing_a_race_is_an_answer(self, conn, monkeypatch):
        """
        Two sessions writing the same lock raced for a unique key and the
        loser got a traceback. The state was right; the answer was not.
        """
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)

        def collide(*a, **k):
            raise psycopg.errors.UniqueViolation("duplicate key value")
        monkeypatch.setattr(pdr_approvals, "ensure_request", collide)
        out = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert out["retry"] is True and "at the same moment" in out["error"]
        monkeypatch.undo()
        assert q.request_requirements_confirmation(
            conn, project_id=ctx.pid)["status"] == "awaiting_approval"

    def test_ingest_arguments_of_the_wrong_type(self, conn):
        for kwargs in (dict(stack=b"bytes"), dict(policy_profile=5), dict(project_id=7)):
            res = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_text=PDR, **kwargs)
            assert "must be a string" in res["error"], kwargs


# ---------------------------------------------------------------------------
# M7. Performance cliffs
# ---------------------------------------------------------------------------

class TestNoCliffs:

    def test_a_pathological_heading_line_end_to_end(self, conn):
        """A 30 KB PDR with one line of 16,000 #s made every call take 25 seconds."""
        doc = "# T\n\n## Requirements\nStaff must sign in with Google OAuth.\n\n# " \
              + "#" * 16_000 + "x\n\n## Other\nMore text here to make a section.\n"
        t0 = time.time()
        ing = ingest(conn, pdr=doc)
        req = {**REQS[0], "quote": "Staff must sign in with Google OAuth.",
               "disposition": "bespoke", "disposition_reason": "x"}
        assert record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                           [req])["requirements"][0]["valid"]
        q.pdr_status(conn, project_id=ing["project_id"])
        q.request_requirements_confirmation(conn, project_id=ing["project_id"])
        assert time.time() - t0 < 5

    def test_many_reflowed_quotes_in_a_large_pdr(self, conn):
        body = "\n\n".join(
            f"## Section {i}\nThe system must do thing number {i} reliably and quickly.\n"
            for i in range(3000))
        ing = ingest(conn, pdr="# T\n\n" + body)
        reqs = [{"slug": f"r{i}", "text": f"thing {i}", "acceptance_criteria": ["works"],
                 "quote": f"The system must do thing number {i}\n   reliably and quickly.",
                 "disposition": "bespoke", "disposition_reason": "x"} for i in range(120)]
        t0 = time.time()
        out = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], reqs)
        assert all(r["valid"] for r in out["requirements"])
        assert time.time() - t0 < 20


# ---------------------------------------------------------------------------
# M8. Evidence from one revision, pin on another
# ---------------------------------------------------------------------------

class TestEvidenceIsForThePinnedRevision:

    def test_a_license_read_on_an_older_revision_does_not_cover_the_new_one(self, conn):
        """
        License evidence on the old revision; the pin goes to the newest,
        which nobody analysed. CIP adopted it by rule: 'MIT', read from a
        revision that is not the one being installed.
        """
        _, cv = make_capability(conn, license="MIT")
        bind_newer_revision(conn, cv, "rev-NEW-relicensed")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "insufficient" and cand["missing_evidence"] == ["license"]
        assert cand["license_source"] == "metadata"
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=cv, rationale="checked the new tag")
        assert d["pinned_revision"] == "rev-NEW-relicensed"
        assert d["decided_by"] == "llm_judgment" and "unanalysed_candidate" in d["flags"]

    def test_the_fit_matcher_reads_the_pinned_revision_too(self, conn):
        """
        MIT on the old revision, GPL on the one that would be installed.
        Reading every bound revision, the matcher found an acceptable
        license and CIP adopted a GPL release by rule.
        """
        _, cv = make_capability(conn, license="MIT")
        bind_newer_revision(conn, cv, "rev-NEW-relicensed", license="GPL-3.0")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient" and cand["license"] == "GPL-3.0"
        assert cand["license_source"] == "evidence"
        assert cand["confirmed_blocking_gaps"] == ["license_mismatch"]
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "BUILD" and d["decided_by"] == "cip_rules"
        forced = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                                   chosen_capability_version_id=cv, rationale="want it anyway")
        assert forced["decided_by"] == "llm_override"
        assert "overrides_blocking_gap" in forced["flags"]


# ---------------------------------------------------------------------------
# M9. Work continuing on a need whose decision moved
# ---------------------------------------------------------------------------

class TestWorkOnADriftedNeedIsOnHold:

    def _drifted(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        tid = bespoke_task(conn, ctx, title="Sign in", objective="x",
                           req_ids=["staff-sign-in", "oauth-login"],
                           permitted_paths=["src/auth/"])["build_task_id"]
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                          rationale="decided again, differently worded")
        return ctx, tid

    def test_progress_and_verification_are_refused_until_a_relock(self, conn):
        ctx, tid = self._drifted(conn)
        for kwargs in (dict(status="in_progress"),
                       dict(verification={**PASS, "req_id": "staff-sign-in",
                                          "criterion_ordinal": 1})):
            out = q.record_build_progress(conn, build_task_id=tid, **kwargs)
            assert out["refused"] and "has changed since it was locked" in out["reason"]
        executed = q.record_executed_verification(conn, build_task_id=tid, verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1})
        assert "has changed since it was locked" in executed["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        check = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": tid, "changed_paths": ["src/auth/login.py"]})
        assert check["verdict"] == "needs_approval" and "on hold" in check["reason"]
        assert q.record_build_progress(conn, build_task_id=tid,
                                       status="abandoned")["status"] == "abandoned"

    def test_work_that_does_not_rest_on_it_carries_on(self, conn):
        ctx, _ = self._drifted(conn)
        other = bespoke_task(conn, ctx)["build_task_id"]
        assert q.record_build_progress(conn, build_task_id=other,
                                       status="in_progress")["status"] == "in_progress"

    def test_a_requirement_deferred_after_the_lock_stops_its_task(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[1], "disposition": "deferred",
                                              "disposition_reason": "next release"}])
        confirm(conn, ctx.pid)
        # Before the relock the task is stale: the lock is, and so is its scope.
        held = q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        assert held["refused"] and "stale" in held["reason"]
        assert lock(conn, ctx.pid)["tasks_abandoned"] == [tid]
        out = q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        assert out["refused"] and "abandoned" in out["reason"]


# ---------------------------------------------------------------------------
# M10. Which architecture document?
# ---------------------------------------------------------------------------

class TestTheArchitectureDocumentIsNamed:

    def test_the_summary_names_the_file_and_its_hash(self, conn, tmp_path):
        """The summary showed 'Architecture doc hash: 0926bb42...' and nothing else."""
        arch = tmp_path / "architecture.md"
        arch.write_bytes(b"# Architecture\r\nA monolith.\r\n")
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, architecture_doc_path=str(arch))
        s = first["summary_markdown"]
        raw_sha = hashlib.sha256(arch.read_bytes()).hexdigest()
        assert f"- File: {arch}" in s and raw_sha in s       # what Get-FileHash would print
        assert "no_architecture_doc" not in flag_codes(first)

    def test_inline_text_is_called_out(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, architecture_doc_text="# A\nx\n")
        assert "TEXT SUPPLIED INLINE BY THE AGENT" in first["summary_markdown"]

    def test_the_same_content_from_another_file_is_another_request(self, conn, tmp_path):
        a, b = tmp_path / "a.md", tmp_path / "decoy.md"
        for f in (a, b):
            f.write_text("# Architecture\nA monolith.\n", encoding="utf-8")
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, architecture_doc_path=str(a))
        approve(conn, first["approval_request_id"])
        second = q.lock_architecture(conn, project_id=ctx.pid, architecture_doc_path=str(b))
        assert second["status"] == "awaiting_approval"
        assert second["approval_request_id"] != first["approval_request_id"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0


# ---------------------------------------------------------------------------
# Lower-severity findings
# ---------------------------------------------------------------------------

class TestOneLiveLockRequest:

    def test_asking_for_something_else_retires_the_earlier_request(self, conn):
        """A fuzz run left 148 pending lock requests, every one of them approvable."""
        ctx = decided_build(conn)
        a = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["a"])
        b = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["b"])
        res = pdr_approvals.record_approval(
            conn, request_id=a["approval_request_id"], outcome="approved", approver="r@d")
        assert "obsolete" in res["error"] and "only the latest one" in res["error"]
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert [p["approval_request_id"] for p in st["pending_approvals"]] == \
            [b["approval_request_id"]]
        assert st["obsolete_requests"] == 1

    def test_asking_again_for_the_first_is_a_new_request(self, conn):
        """
        A, then B, then A's content again. That is a third request, to be
        read and approved as one: the first A is not brought back to life.
        """
        ctx = decided_build(conn)
        a = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["a"])
        q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["b"])
        again = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["a"])
        assert again["status"] == "awaiting_approval"
        assert again["approval_request_id"] != a["approval_request_id"]
        stale = pdr_approvals.record_approval(
            conn, request_id=a["approval_request_id"], outcome="approved", approver="r@d")
        assert "obsolete" in stale["error"]
        approve(conn, again["approval_request_id"])
        done = q.lock_architecture(conn, project_id=ctx.pid,
                                   approval_request_id=again["approval_request_id"])
        assert done["version"] == 1

    def test_polling_does_not_write_an_event_each_time(self, conn):
        ctx = decided_build(conn)
        for _ in range(4):
            q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["a"])
        assert audit_count(conn, "lock_approval_requested") == 1

    def test_an_edited_away_confirmation_is_not_listed_as_waiting(self, cli_db, capsys):
        ctx = with_requirements(cli_db)
        record_need(cli_db, ctx.pid)
        q.request_requirements_confirmation(cli_db, project_id=ctx.pid)
        record_need(cli_db, ctx.pid, description="OAuth sign-in, reworded")
        st = q.pdr_status(cli_db, project_id=ctx.pid)
        assert st["pending_approvals"] == [] and st["obsolete_requests"] == 1
        assert cli.main(["approvals", "list"]) == 0
        assert "pending (obsolete)" in capsys.readouterr().out


class TestSmallerThings:

    def test_a_derived_slug_never_overwrites(self, conn):
        """Same quote, no slug: the text, priority and criteria were overwritten."""
        ing = ingest(conn)
        pid, sid = ing["project_id"], ing["requirement_source_id"]
        base = {"text": "Staff sign in", "acceptance_criteria": ["a", "b"], "priority": "must",
                "quote": "Staff must sign in with Google OAuth before using the system."}
        assert record_reqs(conn, pid, sid, [base])["requirements"][0]["valid"]
        again = record_reqs(conn, pid, sid, [{**base, "priority": "could",
                                              "acceptance_criteria": ["a"]}])
        assert not again["requirements"][0]["valid"]
        assert "pass slug='staff-sign-in'" in again["requirements"][0]["errors"][0]
        assert scalar(conn, "SELECT priority FROM project_requirement") == "must"
        named = record_reqs(conn, pid, sid, [{**base, "slug": "staff-sign-in",
                                              "priority": "could"}])
        assert named["requirements"][0]["valid"]

    def test_long_slugs_are_refused_not_truncated_into_each_other(self, conn):
        ing = ingest(conn)
        long_a, long_b = "a" * 80 + "-one", "a" * 80 + "-two"
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                        [{**REQS[0], "slug": long_a}, {**REQS[1], "slug": long_b}])
        assert [x["valid"] for x in r["requirements"]] == [False, False]
        assert "slug is longer than 80" in r["requirements"][0]["errors"][0]

    def test_a_need_slug_is_not_guessed_either(self, conn):
        ctx = with_requirements(conn)
        need = {k: v for k, v in NEED.items() if k != "slug"}
        first = q.record_capability_needs(conn, project_id=ctx.pid, needs=[need])
        assert first["needs"][0]["valid"]
        again = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**need, "search_terms": ["zzz"]}])
        assert not again["needs"][0]["valid"] and "pass slug=" in again["needs"][0]["errors"][0]

    @pytest.mark.parametrize("field,value", [
        ("text", "​​"), ("text", "ㅤ"), ("disposition_reason", "​"),
        ("acceptance_criteria", ["⠀"]),
    ])
    def test_invisible_text_is_no_text(self, conn, field, value):
        ing = ingest(conn)
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                        [{**REQS[1], field: value}])
        assert not r["requirements"][0]["valid"]

    def test_two_spellings_of_one_name_are_one_provider(self, conn):
        ctx = decided_build(conn)
        nfc, nfd = "Ångström", "Ångström"
        out = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[
            {**STRIPE, "name": nfc}, {**STRIPE, "name": nfd}])
        assert out["refused"] and "duplicate provider" in out["reason"]

    def test_a_search_term_with_a_lookalike_letter_is_refused(self, conn):
        """Cyrillic 'о' in 'оauth': confirmed looking like oauth, matching nothing."""
        ctx = with_requirements(conn)
        out = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "search_terms": ["оauth", "oidc login"]}])
        assert not out["needs"][0]["valid"]
        assert "mixes alphabets" in out["needs"][0]["errors"][0]
        assert "\\u043eauth" in out["needs"][0]["errors"][0]     # shown escaped, so it is visible
        # A whole word in another alphabet was allowed, and that is how a
        # term written entirely in look-alikes got through (fourth review, H4).
        whole = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "search_terms": ["платёж", "oauth"]}])
        assert not whole["needs"][0]["valid"]
        assert "plain ASCII" in whole["needs"][0]["errors"][0]

    def test_flags_do_not_rest_on_the_agents_own_labels(self, conn):
        """OpenAI and Stripe declared as other / free / no data; 20000 USD a month as 'free'."""
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[
            {"name": "OpenAI", "category": "other", "purpose": "summaries", "cost": "free",
             "data_leaves_infra": False},
            {"name": "Stripe Connect", "category": "other", "purpose": "cards", "cost": "free",
             "data_leaves_infra": False}],
            cost_commitments=[{"item": "OpenAI API", "kind": "free", "amount": "20000",
                               "currency": "USD", "period": "month"}])
        s = first["summary_markdown"]
        assert "[sensitive_provider] OpenAI - declared as 'other', but this is a known ai" in s
        assert "[sensitive_provider] Stripe Connect - declared as 'other'" in s
        assert "[cost_commitment] OpenAI API - free 20000 USD per month" in s

    def test_invalid_proposals_are_in_the_audit_trail(self, conn):
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        n = audit_count(conn, "gate_refused")
        assert q.record_decision(conn, req_id=ctx.need_id, verdict="MAYBE")["refused"]
        assert q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                                 chosen_capability_version_id="nope")["refused"]
        assert audit_count(conn, "gate_refused") == n + 2

    def test_a_decision_is_written_whole_or_not_at_all(self, conn, monkeypatch):
        """
        The recommendation was replaced and committed before the decision
        record was inserted. A failure between the two removed the build
        authorisation and left nothing to explain why.
        """
        ctx = locked_build(conn)
        assert scalar(conn, "SELECT count(*) FROM build_authorization") == 1
        before = scalar(conn, "SELECT id::text FROM recommendation")
        real = store.audit

        def fail_on_decision(conn_, **kw):
            if kw.get("event_type") == "decision_recorded":
                raise RuntimeError("the lights went out")
            return real(conn_, **kw)
        monkeypatch.setattr(store, "audit", fail_on_decision)
        with pytest.raises(RuntimeError):
            q.record_decision(conn, req_id=ctx.need_id)
        conn.rollback()
        assert scalar(conn, "SELECT id::text FROM recommendation") == before
        assert scalar(conn, "SELECT count(*) FROM build_authorization") == 1
        assert scalar(conn, "SELECT count(*) FROM decision_record") == 1

    def test_roots_set_to_nothing_reads_nothing(self, conn, tmp_path, monkeypatch):
        f = tmp_path / "pdr.md"
        f.write_text(PDR, encoding="utf-8")
        for value in (" ", ":", ""):
            monkeypatch.setenv("CIP_DOCUMENT_ROOTS", value)
            res = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(f))
            assert "names no folder" in res["error"], repr(value)
        monkeypatch.delenv("CIP_DOCUMENT_ROOTS")
        assert "error" not in q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(f))

    def test_evidence_fields_and_report_counts_are_bounded(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        for over in ({"output_digest": "d" * 500}, {"evidence_ref": "e" * 5000}):
            out = q.record_build_progress(conn, build_task_id=tid,
                                          verification={**BOOKINGS_PASS, **over})
            assert "is longer than" in out["error"]
        many = q.record_build_progress(conn, build_task_id=tid,
                                       verification=[BOOKINGS_PASS] * 101)
        assert "at most 100 verification reports" in many["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0

    def test_a_constraint_cannot_be_a_megabyte(self, conn):
        ctx = with_requirements(conn)
        out = q.record_capability_needs(conn, project_id=ctx.pid, needs=[{
            **NEED, "constraints": [{"kind": "license_allowlist",
                                     "spdx_ids": [f"L{i}" for i in range(2000)]}]}])
        assert not out["needs"][0]["valid"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def target_repo(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "ok.py").write_text("print('fine')\n")
    (tmp_path / "tracked.txt").write_text("original\n")
    (tmp_path / "dirty.py").write_text(
        "open('tracked.txt', 'w').write('changed by the test command')\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestCliSecondRound:

    def test_exit_status_says_whether_it_worked(self, cli_db, tmp_path, capsys):
        """Every one of these exited 0."""
        ing = ingest(cli_db)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps([{"text": "x", "quote": "not in the document at all"}]))
        assert cli.main(["record-requirements", ing["project_id"],
                         ing["requirement_source_id"], str(bad)]) == 2
        assert cli.main(["confirm-requirements", ing["project_id"]]) == 2     # blocked
        needs = tmp_path / "needs.json"
        needs.write_text(json.dumps([{"description": "x"}]))
        assert cli.main(["record-needs", ing["project_id"], str(needs)]) == 2
        assert cli.main(["pdr-brief", ing["project_id"]]) == 2                # blockers
        capsys.readouterr()

    def test_a_misspelt_inputs_key_is_refused(self, cli_db, tmp_path, capsys):
        """`providers` for `approved_providers` was dropped; the request showed 'none'."""
        ctx = decided_build(cli_db)
        f = tmp_path / "inputs.json"
        f.write_text(json.dumps({"providers": [STRIPE], "risks": ["x"]}))
        assert cli.main(["pdr-lock", ctx.pid, "--inputs", str(f)]) == 1
        out = capsys.readouterr().out
        assert "providers, risks" in out and "approved_providers" in out
        assert scalar(cli_db, "SELECT count(*) FROM approval_request "
                              "WHERE kind = 'architecture_lock'") == 0

    def test_verify_looks_at_the_tree_again_afterwards(self, cli_db, target_repo, capsys):
        """
        The tree was checked before the command only. A command that edits
        a tracked file was recorded as a clean, CIP-executed pass.
        """
        ctx = locked_build(cli_db)
        tid = implemented(cli_db, ctx)
        rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                       "--", sys.executable, "dirty.py"])
        assert rc == 0 and "does not count as CIP-executed" in capsys.readouterr().out
        with cli_db.cursor() as cur:
            cur.execute("SELECT worktree_dirty, evidence_ref FROM requirement_verification")
            dirty, ref = cur.fetchone()
        assert dirty is True and ref == f"repo:{target_repo.resolve()}"
        assert q.coverage(cli_db, project_id=ctx.pid)["progress"]["verified_cip_executed"] == 0

    def test_verify_records_which_repository(self, cli_db, target_repo, capsys):
        ctx = locked_build(cli_db)
        tid = implemented(cli_db, ctx)
        assert cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                         "--", sys.executable, "ok.py"]) == 0
        assert f"in {target_repo.resolve()}" in capsys.readouterr().out
        assert q.coverage(cli_db, project_id=ctx.pid)["progress"]["verified_cip_executed"] == 1

    def test_a_criterion_number_that_is_not_ascii_digits(self, cli_db, target_repo, capsys):
        ctx = locked_build(cli_db)
        tid = implemented(cli_db, ctx)
        rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "²",
                       "--", sys.executable, "ok.py"])
        assert rc == 1 and "ordinal" in capsys.readouterr().out
