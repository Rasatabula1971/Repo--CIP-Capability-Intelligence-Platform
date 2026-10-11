"""
Regression tests for the third independent review of the PDR-to-Build
slice. Real Postgres, real tools.

The second review's fixes went to a third reviewer. It found no way to
write a lock, open gate 1 or record an approval without a human approval
for that exact content. It did find nine ways to make CIP or the human
assert something false, twelve bounded faults and nine smaller ones.
Each class here is one finding, named for what must now hold. The
pure-logic side is in test_pdr_core_hardening.py.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from cip_composer import cli
from core.pdr.text import WRAP_PREFIX, display_width
from db.connection import connect
from mcp_server import eval_queries
from mcp_server import pdr_approvals
from mcp_server import pdr_queries as q
from mcp_server import pdr_store as store
from mcp_server import queries as legacy
from mcp_server import server
from tests.pdr_helpers import (
    LICENSE_CONSTRAINT,
    NEED,
    PASS,
    REQS,
    add_dependency,
    add_interface,
    add_license_evidence,
    approve,
    bespoke_task,
    bind_newer_revision,
    confirm,
    decided_adopt,
    decided_build,
    lock,
    locked_adopt,
    locked_build,
    make_capability,
    record_need,
    record_reqs,
    scalar,
    scoped,
    stage_succeeded,
    with_requirements,
)

BOOKINGS_PASS = {**PASS, "req_id": "todays-bookings", "criterion_ordinal": 1}
BOOKINGS_FAIL = {"outcome": "fail", "req_id": "todays-bookings", "criterion_ordinal": 1}
STRIPE = {"name": "Stripe", "category": "payment", "purpose": "billing",
          "cost": "usage", "data_leaves_infra": True}
INTERFACES = [
    {"kind": "license_allowlist", "spdx_ids": ["MIT"]},
    {"kind": "required_interface", "name": "authorize"},
    {"kind": "required_interface", "name": "refresh_token"},
]


def audit_count(conn, event_type: str) -> int:
    return scalar(conn, "SELECT count(*) FROM audit_event WHERE event_type = %s", (event_type,))


def flag_codes(result: dict) -> set[str]:
    return {f["code"] for f in result["human_approval_items"]}


def in_progress(conn, ctx, **task) -> str:
    tid = bespoke_task(conn, ctx, **task)["build_task_id"]
    q.record_build_progress(conn, build_task_id=tid, status="in_progress")
    return tid


def implemented(conn, ctx, **task) -> str:
    tid = in_progress(conn, ctx, **task)
    q.record_build_progress(conn, build_task_id=tid, status="implemented")
    return tid


def verified(conn, ctx) -> str:
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
# H1. "The rules rejected it" reported as "the rules rejected them all"
# ---------------------------------------------------------------------------

class TestNothingFitsIsAboutEveryCandidate:

    def two_candidates(self, conn):
        """
        `mini` ticks every box on paper and has a poor score: best fit, so
        rank 1, and the rules reject it. `library` is the good one: one
        interface short, strong score, ADAPT by the same rules.
        """
        _, library = make_capability(conn, "oauth-library", "OAuth Library",
                                     license="MIT", intrinsic=0.9)
        add_interface(conn, library, "authorize")
        _, mini = make_capability(conn, "oauth-mini", "OAuth mini", license="MIT", intrinsic=0.4)
        add_interface(conn, mini, "authorize")
        add_interface(conn, mini, "refresh_token")
        ctx = scoped(conn, constraints=INTERFACES)
        found = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"]
        assert {c["evidence"] for c in found} == {"sufficient"} and len(found) == 2
        return ctx, library, mini

    def test_cip_does_not_own_a_build_its_rules_would_not_make(self, conn):
        """It was BUILD, cip_rules, no flags, nothing in the lock's attention list."""
        ctx, library, _ = self.two_candidates(conn)
        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "would accept another one" in refused["reason"]
        assert library in refused["reason"]
        assert scalar(conn, "SELECT count(*) FROM decision_record") == 0

    def test_the_agent_can_take_the_candidate_the_rules_accept(self, conn):
        ctx, library, _ = self.two_candidates(conn)
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADAPT",
                              chosen_capability_version_id=library,
                              rationale="mini is unmaintained; the library needs one adapter")
        assert d["verdict"] == "ADAPT" and d["decided_by"] == "llm_judgment"
        assert "judgment:rules_accept_another_candidate" in d["flags"]

    def test_a_build_over_it_is_flagged_for_the_human(self, conn):
        ctx, _, _ = self.two_candidates(conn)
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                              rationale="we want our own")
        assert d["decided_by"] == "llm_judgment"
        assert "build_over_acceptable_candidate" in d["flags"]
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "build_over_acceptable_candidate" in flag_codes(first)
        assert "would accept another" in first["summary_markdown"]

    def test_rejected_every_candidate_is_only_said_when_it_is_true(self, conn):
        _, cv = make_capability(conn, license="GPL-3.0")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "BUILD" and d["decided_by"] == "cip_rules"
        assert "rejected every candidate" in d["note"]


# ---------------------------------------------------------------------------
# H2. A LICENSE file CIP could not identify is not a license it read
# ---------------------------------------------------------------------------

class TestALicenseCipCouldNotName:

    @pytest.mark.parametrize("spdx,confidence", [("unknown", "unknown"), ("GPL-3.0", "low"),
                                                 ("NOASSERTION", None)])
    def test_it_is_missing_evidence_not_a_finding(self, conn, spdx, confidence):
        """`could not tell` became `read it, not allowed`, and BUILD by rule."""
        _, cv = make_capability(conn, metadata_license="MIT")
        add_license_evidence(conn, cv, spdx, confidence)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "insufficient" and cand["missing_evidence"] == ["license"]
        assert cand["confirmed_blocking_gaps"] == []
        assert cand["license"] == "MIT" and cand["license_source"] == "metadata"
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]

    def test_an_identified_license_still_is_one(self, conn):
        _, cv = make_capability(conn)
        add_license_evidence(conn, cv, "GPL-3.0", "high")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient"
        assert cand["confirmed_blocking_gaps"] == ["license_mismatch"]
        assert cand["license"] == "GPL-3.0" and cand["license_source"] == "evidence"


# ---------------------------------------------------------------------------
# H3. What the human confirms as search terms is what the registry is asked
# ---------------------------------------------------------------------------

class TestWhatIsConfirmedIsWhatIsAsked:

    @pytest.mark.parametrize("terms,why", [
        (["oauth, oidc, sso, saml"], "contains ','"),
        (["oau︎th"], "invisible character"),
        (["oauth; drop"], "contains ';'"),
        (["100%"], "contains '%'"),
        (["o_auth"], "contains '_'"),
        (['say "oauth"'], "contains"),
    ])
    def test_a_term_is_plain_and_single(self, conn, terms, why):
        """
        One term holding a comma-separated list displayed exactly like the
        list and found nothing: BUILD, cip_rules, 'searched: oauth, oidc...'.
        """
        ctx = with_requirements(conn)
        out = q.record_capability_needs(conn, project_id=ctx.pid,
                                        needs=[{**NEED, "search_terms": terms}])
        assert not out["needs"][0]["valid"] and why in out["needs"][0]["errors"][0]

    def test_ordinary_terms_still_pass(self, conn):
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid, search_terms=["c++", "node.js", "ci/cd", "oidc login",
                                                        "pdf-render", "F#"])
        assert need["valid"]

    def test_each_term_is_shown_as_one_term(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid, search_terms=["oauth", "oidc login"])
        summary = q.request_requirements_confirmation(conn, project_id=ctx.pid)["summary_markdown"]
        assert '- will be searched for (2 term(s)): "oauth", "oidc login"' in summary

    def test_a_decorated_name_does_not_pass_the_provider_floor(self, conn):
        ctx = decided_build(conn)
        hidden = {"name": "Str︎ipe", "category": "other", "purpose": "",
                  "cost": "free", "data_leaves_infra": False}
        refused = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[hidden])
        assert refused["refused"] and "plain text in one alphabet" in refused["reason"]
        spaced = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[
            {**hidden, "name": "S t r i p e"}, {**hidden, "name": "Open-AI gateway"}])
        details = [f["detail"] for f in spaced["human_approval_items"]
                   if f["code"] == "sensitive_provider"]
        assert len(details) == 2 and "known payment provider" in " ".join(details)


# ---------------------------------------------------------------------------
# H4. Interfaces and dependencies are read for the pinned revision too
# ---------------------------------------------------------------------------

class TestEveryKindOfEvidenceIsThePinnedRevisions:

    @pytest.mark.parametrize("constraint,missing", [
        ({"kind": "required_interface", "name": "authorize"}, "interfaces"),
        ({"kind": "forbidden_dependency", "ecosystem": "pypi", "name": "cryptography"},
         "dependencies"),
    ])
    def test_what_was_read_on_an_older_revision_does_not_answer_for_the_new_one(
            self, conn, constraint, missing):
        """It was `sufficient`, ADOPT by rule, pinned to a revision nobody opened."""
        _, cv = make_capability(conn, license="MIT", intrinsic=0.9)
        add_interface(conn, cv, "authorize")
        add_dependency(conn, cv, "pypi", "requests")
        bind_newer_revision(conn, cv, "rev-NEW")
        ctx = scoped(conn, constraints=[constraint])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "insufficient" and cand["missing_evidence"] == [missing]
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]

    def test_the_matcher_does_not_find_an_interface_the_new_revision_dropped(self, conn):
        """R1 had `authorize`. R2 was analysed and has no such interface."""
        _, cv = make_capability(conn, license="MIT", intrinsic=0.9)
        add_interface(conn, cv, "authorize")
        bind_newer_revision(conn, cv, "rev-NEW", license="MIT")
        stage_succeeded(conn, cv, "interfaces")
        ctx = scoped(conn, constraints=[{"kind": "required_interface", "name": "authorize"}])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient" and cand["fit_score"] == 0.0
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "BUILD" and d["decided_by"] == "cip_rules"

    def test_the_matcher_does_not_hold_an_old_dependency_against_the_new_revision(self, conn):
        """R1 depended on the forbidden package. R2's manifest was read and does not."""
        _, cv = make_capability(conn, license="MIT", intrinsic=0.9)
        add_dependency(conn, cv, "pypi", "cryptography")
        bind_newer_revision(conn, cv, "rev-NEW", license="MIT")
        stage_succeeded(conn, cv, "manifests")
        ctx = scoped(conn, constraints=[
            {"kind": "forbidden_dependency", "ecosystem": "pypi", "name": "cryptography"}])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient" and cand["confirmed_blocking_gaps"] == []
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "ADOPT" and d["decided_by"] == "cip_rules"

    def test_the_same_evidence_on_the_pinned_revision_counts(self, conn):
        _, cv = make_capability(conn, license="MIT", intrinsic=0.9)
        add_interface(conn, cv, "authorize")
        add_dependency(conn, cv, "pypi", "requests")
        ctx = scoped(conn, constraints=[
            {"kind": "required_interface", "name": "authorize"},
            {"kind": "forbidden_dependency", "ecosystem": "pypi", "name": "cryptography"}])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient" and cand["fit_score"] == 1.0
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "ADOPT" and d["decided_by"] == "cip_rules"


# ---------------------------------------------------------------------------
# H5. The lock shows the license of the revision it pins
# ---------------------------------------------------------------------------

class TestTheLockShowsThePinnedRevisionsLicense:

    def test_a_newer_binding_does_not_change_what_the_lock_says(self, conn):
        """
        Decided on R1 (GPL, read from source). R2 (MIT) is bound afterwards.
        The lock read 'pinned: R1 / license: MIT' and the GPL flag was gone.
        """
        _, cv = make_capability(conn)
        add_license_evidence(conn, cv, "GPL-3.0", "high")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=cv, rationale="we accept the GPL")
        assert d["pinned_revision"] == "rev-oauth-library"
        bind_newer_revision(conn, cv, "rev-NEW-mit", license="MIT")

        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "non_permissive_license" in flag_codes(first)
        s = first["summary_markdown"]
        assert "    pinned: test/oauth-library @rev-oauth-library" in s
        assert "    license: GPL-3.0 (read from source)" in s
        assert "license: MIT" not in s

    def test_and_the_other_way_round(self, conn):
        ctx = decided_adopt(conn)                                    # MIT on R1
        bind_newer_revision(conn, ctx.cv_id, "rev-NEW-gpl", license="GPL-3.0")
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert "non_permissive_license" not in flag_codes(first)
        assert "    license: MIT (read from source)" in first["summary_markdown"]


# ---------------------------------------------------------------------------
# H6. Only the human's latest word on the lock can become the lock
# ---------------------------------------------------------------------------

class TestOnlyTheLatestLockRequestIsWritten:

    def test_an_approval_overtaken_by_a_later_request_cannot_be_written(self, conn):
        """
        A (with Stripe) approved. The human changes their mind; B (without)
        is requested and approved. The agent wrote A.
        """
        ctx = decided_build(conn)
        a = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[STRIPE])
        approve(conn, a["approval_request_id"])
        b = q.lock_architecture(conn, project_id=ctx.pid)
        approve(conn, b["approval_request_id"])
        assert q.pdr_status(conn, project_id=ctx.pid)["approved_lock_request"][
            "approval_request_id"] == b["approval_request_id"]

        old = q.lock_architecture(conn, project_id=ctx.pid,
                                  approval_request_id=a["approval_request_id"])
        assert old["refused"] and "later lock request" in old["reason"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0

        done = q.lock_architecture(conn, project_id=ctx.pid,
                                   approval_request_id=b["approval_request_id"])
        assert done["version"] == 1
        assert q.pdr_status(conn, project_id=ctx.pid)["lock"]["approved_providers"] == []

    def test_asking_for_the_old_content_again_needs_a_new_approval(self, conn):
        """Supplying A's inputs again must not ride on A's old approval either."""
        ctx = decided_build(conn)
        a = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[STRIPE])
        approve(conn, a["approval_request_id"])
        b = q.lock_architecture(conn, project_id=ctx.pid)
        approve(conn, b["approval_request_id"])
        again = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[STRIPE])
        assert again["status"] == "awaiting_approval"
        assert again["approval_request_id"] not in (a["approval_request_id"],
                                                    b["approval_request_id"])
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0
        # And B, approved, is now overtaken in its turn.
        late = q.lock_architecture(conn, project_id=ctx.pid,
                                   approval_request_id=b["approval_request_id"])
        assert late["refused"]

    def test_the_ordinary_two_call_flow_is_unchanged(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["r"])
        same = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["r"])
        assert same["approval_request_id"] == first["approval_request_id"]
        assert audit_count(conn, "lock_approval_requested") == 1
        approve(conn, first["approval_request_id"])
        by_inputs = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["r"])
        assert by_inputs["version"] == 1

    def test_the_request_history_is_part_of_what_is_hashed(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        relock = lock(conn, ctx.pid, unresolved_risks=["x"])
        row = store.load_lock(conn, relock["architecture_lock_id"])
        assert row["follows_request_id"] is not None
        assert store.lock_stale_reasons(row, row["requirements_hash"], "approved") == []
        with conn.cursor() as cur, pytest.raises(psycopg.errors.RaiseException):
            cur.execute("UPDATE architecture_lock SET follows_request_id = NULL WHERE id = %s",
                        (relock["architecture_lock_id"],))
        conn.rollback()


# ---------------------------------------------------------------------------
# H7, M3. A fail stands; a pass stands while its task does
# ---------------------------------------------------------------------------

class TestAFailIsNotErasedByAbandoningTheTask:

    def test_the_older_pass_does_not_come_back(self, conn):
        """
        T0 passed. T1, on the same requirement, observed a fail. Abandon T1,
        and coverage showed T0's pass again: verified, CIP-executed.
        """
        ctx = locked_build(conn)
        t0 = verified(conn, ctx)
        t1 = in_progress(conn, ctx, title="Bookings: pagination")
        q.record_build_progress(conn, build_task_id=t1, verification=BOOKINGS_FAIL)
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 0

        q.record_build_progress(conn, build_task_id=t1, status="abandoned")
        row = next(r for r in q.coverage(conn, project_id=ctx.pid)["by_requirement"]
                   if r["slug"] == "todays-bookings")
        assert row["verification_outcome"] == "fail" and not row["verified"]
        again = q.record_build_progress(conn, build_task_id=t0, status="verified")
        assert again["refused"] and "(fail)" in again["reason"]

        fixed = q.record_build_progress(conn, build_task_id=t0, status="verified",
                                        verification=BOOKINGS_PASS)
        assert fixed["status"] == "verified"

    def test_verified_is_not_granted_on_a_pass_from_a_dead_task(self, conn):
        """The write path counted it and stored `verified`; every read said no."""
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        task = dict(objective="x", req_ids=["staff-sign-in", "oauth-login"],
                    permitted_paths=["src/auth/"])
        old = implemented(conn, ctx, title="auth", **task)
        q.record_build_progress(conn, build_task_id=old, verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 2})
        q.record_build_progress(conn, build_task_id=old, status="abandoned")

        new = implemented(conn, ctx, title="auth v2", **task)
        out = q.record_build_progress(conn, build_task_id=new, status="verified", verification={
            **PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1})
        assert out["refused"] and out["status"] == "implemented"
        assert "staff-sign-in #2 (no verification recorded)" in out["reason"]
        assert scalar(conn, "SELECT status FROM build_task WHERE id = %s", (new,)) == "implemented"

    def test_a_refused_verified_keeps_the_reports_and_is_logged_as_a_task_refusal(self, conn):
        ctx = locked_build(conn)
        tid = implemented(conn, ctx)
        before = audit_count(conn, "task_refused")
        out = q.record_build_progress(conn, build_task_id=tid, status="verified",
                                      verification=BOOKINGS_FAIL)
        assert out["refused"] and len(out["verifications"]) == 1
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 1
        assert audit_count(conn, "task_refused") == before + 1


# ---------------------------------------------------------------------------
# H8. The legacy import tool stays out of PDR projects
# ---------------------------------------------------------------------------

class TestTheLegacyImportToolDoesNotRewritePdrRows:

    YAML = ("requirements:\n  - slug: todays-bookings\n    description: Something else entirely\n"
            "    constraints:\n      - kind: license_allowlist\n        spdx_ids: [GPL-3.0]\n")

    def test_it_is_refused_for_a_project_with_a_pdr(self, conn):
        """
        It changed a verified requirement's text and kept the old pass,
        took a 300,000-character description, and put constraints on a PDR
        requirement that were neither hashed nor shown.
        """
        ctx = locked_build(conn)
        verified(conn, ctx)
        before = scalar(conn, "SELECT description FROM project_requirement WHERE slug = %s",
                        ("todays-bookings",))
        out = legacy.import_requirements(conn, ctx.pid, self.YAML)
        assert "PDR-to-Build workflow" in out["error"] and out["updated"] == 0
        assert scalar(conn, "SELECT description FROM project_requirement WHERE slug = %s",
                      ("todays-bookings",)) == before
        assert scalar(conn, "SELECT count(*) FROM requirement_constraint rc "
                            "JOIN project_requirement pr ON pr.id = rc.project_requirement_id "
                            "WHERE pr.slug = 'todays-bookings'") == 0
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 1

    def test_it_still_works_for_a_project_without_one(self, conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO project (name) VALUES ('plain') RETURNING id")
            pid = str(cur.fetchone()[0])
        conn.commit()
        out = legacy.import_requirements(conn, pid, self.YAML)
        assert out.get("created") == 1, out


# ---------------------------------------------------------------------------
# H9, M11. An anchor must satisfy the rules against the text it points into
# ---------------------------------------------------------------------------

REAL = ("# Shop PDR\n\n## Checkout\nCustomers pay by card.\n\n## Refunds\n"
        "Refunds are made within 30 days.\n\n## Audit\nEvery change is logged for a year.\n")
FLAT = "Shop PDR Checkout Customers pay by card. Refunds Refunds are made within 30 days."


class TestAnAnchorIsCheckedAgainstTheCurrentText:

    def test_a_quote_is_not_carried_across_a_reingest_onto_several_sections(self, conn, tmp_path):
        """
        Recorded against a flattened inline copy with no headings, then the
        real file was ingested: carried forward, every section 'quoted'.
        """
        # The same words on one line: "## Refunds" is mid-line, so not a heading.
        flat = " ".join(REAL.split())
        ing = q.ingest_pdr(conn, project_name="shop", pdr_title="Shop", pdr_text=flat)
        quote = ("Customers pay by card. ## Refunds Refunds are made within 30 days. "
                 "## Audit Every change is logged for a year.")
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "pay", "text": "Pay and refund", "quote": quote,
            "acceptance_criteria": ["a card payment succeeds"],
            "disposition": "bespoke", "disposition_reason": "x"}])
        assert rr["requirements"][0]["valid"], rr

        # In the real text the same words are there, whitespace apart, across
        # three sections. Refused if recorded against it directly:
        from core.pdr import requirements as req_rules
        _, errors = req_rules.validate_requirement_draft(
            {"text": "t", "quote": quote, "acceptance_criteria": ["c"]}, REAL)
        assert "runs across the heading" in errors[0]
        # ...and so not carried onto it either.
        again = q.ingest_pdr(conn, project_name="shop", pdr_title="Shop", pdr_text=REAL)
        assert again["orphaned"] == ["pay"] and again["carried_forward"] == []
        rc = q.request_requirements_confirmation(conn, project_id=ing["project_id"])
        assert rc["status"] == "blocked"

    def test_a_quote_inside_one_section_is_still_carried(self, conn):
        ing = q.ingest_pdr(conn, project_name="shop", pdr_title="Shop", pdr_text=REAL)
        record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "pay", "text": "Pay", "quote": "Customers pay by card.",
            "acceptance_criteria": ["a card payment succeeds"],
            "disposition": "bespoke", "disposition_reason": "x"}])
        again = q.ingest_pdr(conn, project_name="shop", pdr_title="Shop",
                             pdr_text=REAL + "\n## Later\nMore to come.\n")
        assert again["carried_forward"] == ["pay"] and again["orphaned"] == []

    ACROSS = "Customers pay by card.\n\n## Refunds\nRefunds are made"

    @pytest.mark.parametrize("span,blocker", [
        ({"start": 0, "end": 40, "quote": "The system shall be delightful to use."},
         "is not what the PDR says at that place"),
        ({"start": REAL.index(ACROSS), "end": REAL.index(ACROSS) + len(ACROSS), "quote": ACROSS},
         "runs across a section heading"),
    ])
    def test_a_stored_anchor_that_breaks_the_rules_blocks_confirmation(self, conn, span, blocker):
        """
        A row migrated from v1.0 keeps the offsets and the quote its caller
        made up. Once the text was filled in, the human was shown a `PDR:`
        line that is not in the PDR, and its span mapped every section.
        """
        ing = q.ingest_pdr(conn, project_name="shop", pdr_title="Shop", pdr_text=REAL)
        record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "pay", "text": "Pay", "quote": "Customers pay by card.",
            "acceptance_criteria": ["a card payment succeeds"],
            "disposition": "bespoke", "disposition_reason": "x"}])
        with conn.cursor() as cur:
            cur.execute("UPDATE project_requirement SET source_span = %s::jsonb "
                        "WHERE slug = 'pay'", (json.dumps(span),))
        conn.commit()
        rc = q.request_requirements_confirmation(conn, project_id=ing["project_id"])
        assert rc["status"] == "blocked" and blocker in " ".join(rc["blockers"])
        cov = rc["span_coverage"]
        assert cov["mapped_section_count"] <= 2


# ---------------------------------------------------------------------------
# M1. The architecture document is checked again at approval
# ---------------------------------------------------------------------------

class TestTheArchitectureDocumentIsCheckedAtApproval:

    def test_a_file_changed_after_the_request_is_called_out(self, cli_db, tmp_path, capsys):
        doc = tmp_path / "ARCHITECTURE.md"
        doc.write_text("# Architecture\nA monolith.\n", encoding="utf-8")
        ctx = decided_build(cli_db)
        first = q.lock_architecture(cli_db, project_id=ctx.pid, architecture_doc_path=str(doc))
        rid = first["approval_request_id"]
        assert "on disk matches" in q.describe_request(cli_db, rid)["doc_check"]

        doc.write_text("# Architecture\nMicroservices, and a blockchain.\n", encoding="utf-8")
        check = q.describe_request(cli_db, rid)["doc_check"]
        assert check.startswith("WARNING:") and "has changed since" in check
        assert cli.main(["approvals", "show", rid]) == 0
        assert "Architecture document: WARNING:" in capsys.readouterr().out

    def test_inline_text_has_no_file_to_check(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, architecture_doc_text="# A\nB\n")
        assert q.describe_request(conn, first["approval_request_id"])["doc_check"] is None


# ---------------------------------------------------------------------------
# M2. A file a CIP tool wrote is not presented as a document someone supplied
# ---------------------------------------------------------------------------

class TestAFileWrittenByATool:

    def test_the_summary_says_a_tool_wrote_it(self, conn, tmp_path, monkeypatch):
        """
        scaffold_pipeline writes README.md with the caller's text. Ingested,
        it was shown with a path, a SHA-256 and 'on disk matches', and never
        as text supplied by the agent.
        """
        monkeypatch.setattr(server, "connect", lambda: connect(for_tests=True))
        body = ("\n\n## Requirements\n1. Staff must sign in with Google OAuth before using "
                "the system.\n")
        out = server.scaffold_pipeline(
            {"name": "Clinic PDR", "intent": "Reduce missed calls.", "stages": [], "edges": [],
             "notes": ["agreed with the client" + body]}, str(tmp_path / "docs"))
        assert "README.md" in out["files_written"]
        assert audit_count(conn, "file_written_by_tool") == len(out["files_written"])

        ing = q.ingest_pdr(conn, project_name="clinic", pdr_title="Clinic PDR",
                           pdr_path=str(tmp_path / "docs" / "README.md"))
        record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "sign-in", "text": "Staff sign in",
            "quote": "Staff must sign in with Google OAuth before using the system.",
            "acceptance_criteria": ["signs in"], "disposition": "bespoke",
            "disposition_reason": "x"}])
        summary = q.request_requirements_confirmation(
            conn, project_id=ing["project_id"])["summary_markdown"]
        assert "THIS FILE WAS WRITTEN BY A CIP TOOL (scaffold_pipeline)" in summary

    def test_a_file_someone_else_wrote_is_not_marked(self, conn, tmp_path):
        pdr = tmp_path / "PDR.md"
        pdr.write_text("# P\n\nStaff must sign in with Google OAuth before using the system.\n",
                       encoding="utf-8")
        ing = q.ingest_pdr(conn, project_name="clinic", pdr_title="P", pdr_path=str(pdr))
        record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "sign-in", "text": "Staff sign in",
            "quote": "Staff must sign in with Google OAuth before using the system.",
            "acceptance_criteria": ["signs in"], "disposition": "bespoke",
            "disposition_reason": "x"}])
        summary = q.request_requirements_confirmation(
            conn, project_id=ing["project_id"])["summary_markdown"]
        assert "WRITTEN BY A CIP TOOL" not in summary


# ---------------------------------------------------------------------------
# M4. Drift, and what "complete" means
# ---------------------------------------------------------------------------

class TestDriftAndCompleteness:

    def drifted(self, conn):
        ctx = locked_adopt(conn)
        tid = implemented(conn, ctx, title="auth", objective="x",
                          req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"])
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                              rationale="changed our minds")
        assert d["decided_by"] == "llm_override"
        return ctx, tid

    def test_the_lock_check_holds_a_drifted_need_without_a_path(self, conn):
        """It was `allowed` unless changed_paths was sent too."""
        ctx, tid = self.drifted(conn)
        cap = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "capability_changes": [{"need": "oauth-login", "verdict": "ADOPT"}]})
        assert cap["verdict"] == "needs_approval" and "has changed since lock v1" in cap["reason"]
        dep = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "build_task_id": tid,
            "dependency_adds": [{"ecosystem": "pypi", "name": "oauth-library"}]})
        assert dep["verdict"] == "needs_approval" and "Relock first" in dep["reason"]

    def test_a_project_is_not_complete_while_a_locked_decision_has_moved(self, conn):
        ctx = locked_adopt(conn)
        verified(conn, ctx)
        t2 = implemented(conn, ctx, title="auth", objective="x",
                         req_ids=["staff-sign-in", "oauth-login"], permitted_paths=["src/auth/"])
        done = q.record_build_progress(conn, build_task_id=t2, status="verified", verification=[
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 1},
            {**PASS, "req_id": "staff-sign-in", "criterion_ordinal": 2}])
        assert done["status"] == "verified"
        assert q.pdr_status(conn, project_id=ctx.pid)["phase"] == "complete"

        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="changed our minds")
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["phase"] == "building" and st["progress"]["verified"] == 1
        assert "decisions changed since the lock" in st["next_actions"][0]

    def test_a_pass_is_not_recorded_against_work_nobody_started(self, conn):
        """Phase was `complete` with every task still planned and nothing implemented."""
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        out = q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_PASS)
        assert out["refused"] and "still planned" in out["reason"]
        executed = q.record_executed_verification(conn, build_task_id=tid,
                                                  verification=BOOKINGS_PASS)
        assert "still planned" in executed["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        # A fail is true whatever the task's status.
        failed = q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_FAIL)
        assert "refused" not in failed
        # Starting and passing in one call is fine.
        both = q.record_build_progress(conn, build_task_id=tid, status="in_progress",
                                       verification=BOOKINGS_PASS)
        assert both["status"] == "in_progress" and len(both["verifications"]) == 1

    def test_verified_implies_implemented(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_PASS)
        p = q.coverage(conn, project_id=ctx.pid)["progress"]
        assert (p["tasked"], p["implemented"], p["verified"]) == (1, 0, 0)
        row = next(r for r in q.coverage(conn, project_id=ctx.pid)["by_requirement"]
                   if r["slug"] == "todays-bookings")
        assert row["verification_outcome"] == "pass" and row["verified"] is False


# ---------------------------------------------------------------------------
# M5. A request that can no longer be written is not "waiting"
# ---------------------------------------------------------------------------

class TestDeadRequestsAreNotLive:

    def test_an_approved_request_the_project_has_moved_past_is_not_offered(self, conn):
        """Status said ready_to_lock and 'write it'; writing it was refused."""
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        approve(conn, first["approval_request_id"])
        assert q.pdr_status(conn, project_id=ctx.pid)["approved_lock_request"] is not None

        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="decided again")
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["approved_lock_request"] is None
        assert st["phase"] == "ready_to_lock" and "approval_request_id" not in st["next_actions"][0]

        fresh = q.lock_architecture(conn, project_id=ctx.pid)
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["phase"] == "awaiting_lock_approval"
        assert [p["approval_request_id"] for p in st["pending_approvals"]] == [
            fresh["approval_request_id"]]

    def test_a_pending_lock_request_dies_when_the_project_moves(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        record_need(conn, ctx.pid, description="OAuth sign-in, reworded")
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["pending_approvals"] == [] and st["obsolete_requests"] == 1
        described = q.describe_request(conn, first["approval_request_id"])
        assert "can no longer be locked" in described["obsolete_reason"]

    def test_a_pending_relock_is_mentioned_while_building(self, conn):
        ctx = locked_build(conn)
        q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["new"])
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["phase"] == "building"
        assert "a relock request is waiting for the human" in st["next_actions"][0]


# ---------------------------------------------------------------------------
# M6. What the human approved can be written
# ---------------------------------------------------------------------------

class TestAnApprovedLockCanBeWritten:

    def test_a_judged_build_over_a_truncated_empty_sweep_passes_step_13(self, conn, monkeypatch):
        """
        100 matches, none evaluable: CIP will not decide, the agent judges
        BUILD, the human approves — and Step 13 refused every write, because
        the lock had replaced the 'searched and found nothing' marker.
        """
        monkeypatch.setattr(q, "SWEEP_LIMIT", 3)
        for i in range(3):
            make_capability(conn, f"oauth-thing-{i}", f"OAuth thing {i}", bind=False)
        ctx = scoped(conn)
        res = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert res["truncated"] and res["candidate_count"] == 0
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale="none usable")
        assert d["decided_by"] == "llm_judgment"
        written = lock(conn, ctx.pid)
        assert written["version"] == 1 and written["build_authorizations"] == 1
        assert scalar(conn, "SELECT rule_name FROM recommendation") == "__no_candidates__"


# ---------------------------------------------------------------------------
# M7. The banked-rewrite flag uses the search's own test
# ---------------------------------------------------------------------------

def bank(conn, **over):
    args = dict(asset_slug="authlib-eval", verdict="Adopt", evidence_depth="code_read",
                banked=False, summary="OAuth flow kit", problem_types=["oauth-flow"])
    args.update(over)
    res = eval_queries.record_evaluation(conn, **args)
    assert not res.get("error"), res
    return res


class TestARewrittenEvaluationIsNoticed:

    def test_a_usable_unbanked_evaluation_rewritten_before_the_first_sweep(self, conn):
        """Search counts any non-Reject tag match; the flag required `banked`."""
        ctx = scoped(conn)
        bank(conn)
        bank(conn, verdict="Reject", evidence_depth="docs_only")
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert "banked_evaluation_changed" in d["flags"]

    def test_one_rewritten_shortly_before_the_project_existed(self, conn):
        bank(conn, banked=True)
        bank(conn, verdict="Reject", banked=False)
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert "banked_evaluation_changed" in d["flags"]

    def test_an_old_rewrite_is_not_held_against_every_later_project(self, conn):
        bank(conn, banked=True)
        bank(conn, verdict="Reject", banked=False)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE audit_event DISABLE RULE audit_event_no_update")
            cur.execute("UPDATE audit_event SET created_at = now() - interval '90 days' "
                        "WHERE event_type = 'evaluation_replaced'")
            cur.execute("ALTER TABLE audit_event ENABLE RULE audit_event_no_update")
        conn.commit()
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        assert "banked_evaluation_changed" not in q.record_decision(
            conn, req_id=ctx.need_id)["flags"]


# ---------------------------------------------------------------------------
# M8. The sweep has to describe the registry as it is
# ---------------------------------------------------------------------------

class TestTheSweepIsFresh:

    def test_ingesting_an_existing_match_asks_for_a_new_sweep(self, conn):
        """
        The match the sweep could not evaluate gets its source bound and
        analysed. No capability was added, so the count check passed and the
        decision rested on the old sweep: BUILD, cip_rules.
        """
        _, cv = make_capability(conn, bind=False)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        assert q.search_for_requirement(conn, req_id=ctx.need_id)[
            "coverage_outcome"] == "found_but_unevaluable"
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM source_revision LIMIT 1")
            cur.execute("INSERT INTO capability_source_binding (capability_version_id, "
                        "source_revision_id) VALUES (%s, %s)", (cv, cur.fetchone()[0]))
        conn.commit()
        add_license_evidence(conn, cv, "MIT", "high")

        stale = q.record_decision(conn, req_id=ctx.need_id)
        assert stale["refused"] and "registry has changed" in stale["reason"]
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "ADOPT" and d["decided_by"] == "cip_rules"

    def test_a_metadata_edit_counts_as_a_change(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        with conn.cursor() as cur:
            cur.execute("UPDATE capability SET display_name = 'Renamed'")
        conn.commit()
        assert q.record_decision(conn, req_id=ctx.need_id)["refused"]

    def test_nothing_changed_nothing_asked(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        assert q.record_decision(conn, req_id=ctx.need_id)["verdict"] == "ADOPT"
        again = q.record_decision(conn, req_id=ctx.need_id)
        assert again["verdict"] == "ADOPT"


# ---------------------------------------------------------------------------
# M10. No row on the screen is the agent's
# ---------------------------------------------------------------------------

class TestEveryLineIsWrapped:

    def test_a_name_cannot_fill_a_row(self, conn):
        long_name = "clinic " + "." * 60
        out = q.ingest_pdr(conn, project_name=long_name, pdr_title="T", pdr_text="# T\n\nBody.\n")
        assert "longer than 60 characters" in out["error"]

    def test_lines_outside_the_summary_are_wrapped_too(self, cli_db, monkeypatch, capsys):
        """
        `Project: <name>` was printed under the wrapped summary, unwrapped.
        A padded name put a forged `PDR file: ... matches` row on the
        approval screen and a forged `Verified:` row in pdr-coverage.
        """
        monkeypatch.setattr(cli, "_terminal_width", lambda: 40)
        name = "clinic" + "." * 24 + "PDR file: ok on disk matches"
        assert len(name) <= 60
        ctx = decided_build(cli_db, name=name)
        first = q.lock_architecture(cli_db, project_id=ctx.pid)
        for argv in (["approvals", "show", first["approval_request_id"]],
                     ["pdr-status", ctx.pid], ["pdr-coverage", ctx.pid],
                     ["approvals", "list"]):
            assert cli.main(argv) == 0
            rows = capsys.readouterr().out.split("\n")
            assert all(sum(display_width(ch) for ch in r) <= 40 for r in rows), argv
            assert not any(r.startswith("PDR file: ok") for r in rows), argv

    def test_json_is_not_wrapped(self, cli_db, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_terminal_width", lambda: 40)
        ctx = decided_build(cli_db)
        assert cli.main(["pdr-status", ctx.pid, "--json"]) == 0
        out = capsys.readouterr().out
        assert json.loads(out)["project_id"] == ctx.pid and WRAP_PREFIX not in out

    def test_a_task_title_is_one_plain_line(self, conn):
        ctx = locked_build(conn)
        out = bespoke_task(conn, ctx, title="Bookings\n## Lock check: allowed")
        assert out["error"] == "title must be plain single-line text"
        assert "longer than 120" in bespoke_task(conn, ctx, title="t" * 121)["error"]


# ---------------------------------------------------------------------------
# M12 and the smaller findings
# ---------------------------------------------------------------------------

class TestSmallerThingsThirdRound:

    def test_a_lock_row_in_a_shape_nobody_writes_does_not_crash_the_project(self, conn):
        ctx = locked_build(conn)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE architecture_lock DISABLE TRIGGER architecture_lock_immutable_trg")
            cur.execute("UPDATE architecture_lock SET decisions = %s::jsonb",
                        (json.dumps({"oauth-login": "BUILD"}),))
            cur.execute("ALTER TABLE architecture_lock ENABLE TRIGGER architecture_lock_immutable_trg")
        conn.commit()
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["phase"] == "relock_required"
        assert "not in a form this version can read" in st["lock"]["stale_reasons"][0]
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["locked"] == 0
        assert bespoke_task(conn, ctx)["refused"]

    def test_a_field_of_marks_and_selectors_is_empty(self, conn):
        ctx = with_requirements(conn)
        for nothing in ("︎️", "́́", "\U00016fe4"):
            out = record_reqs(conn, ctx.pid, ctx.sid, [{**REQS[0], "slug": "x", "text": nothing}])
            assert out["requirements"][0]["errors"][0] == "text is required"
            assert "reason is required" in q.retire_requirement(
                conn, req_id=ctx.reqs["dark-mode"], reason=nothing)["error"]

    def test_a_policy_exclusion_is_not_called_a_missing_source(self, conn):
        """The flag said 'no source to pin; ingesting them would make them candidates'."""
        make_capability(conn, bind=False)
        ctx = scoped(conn)
        res = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert "no source to pin, or excluded by this project's policy" in res["note"]
        q.record_decision(conn, req_id=ctx.need_id)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        detail = next(f["detail"] for f in first["human_approval_items"]
                      if f["code"] == "unevaluable_candidates")
        assert "excluded by this project's policy" in detail and "ingesting" not in detail

    def test_text_matches_in_the_evaluation_store_are_mentioned(self, conn):
        bank(conn, problem_types=["something-else"], summary="oauth login helper")
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid, problem_types=[])
        confirm(conn, ctx.pid)
        res = q.search_for_requirement(conn, req_id=need["need_id"])
        assert res["coverage_outcome"] == "no_registry_coverage"
        assert "match on text only" in res["note"] and "no problem_types" in res["note"]

    def test_the_confirmation_says_when_a_need_has_no_tags(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid, problem_types=[])
        summary = q.request_requirements_confirmation(conn, project_id=ctx.pid)["summary_markdown"]
        assert "problem types: none (banked evaluations cannot be matched" in summary

    def test_two_terminals_deciding_one_request(self, conn, monkeypatch):
        """Both read `pending`; the second insert lost on the unique key and raised."""
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        rid = first["approval_request_id"]
        seen_pending = pdr_approvals.get_request(conn, rid)
        conn.commit()
        other = connect(for_tests=True)
        try:
            approve(other, rid)
        finally:
            other.close()
        # The second terminal still holds the view it read before the first decided.
        monkeypatch.setattr(pdr_approvals, "get_request", lambda c, r: dict(seen_pending))
        res = pdr_approvals.record_approval(
            conn, request_id=rid, outcome="approved", approver="second@desk")
        assert "decided a moment ago" in res["error"]
        assert scalar(conn, "SELECT count(*) FROM approval WHERE approval_request_id = %s",
                      (rid,)) == 1

    def test_the_override_reaches_the_human_with_both_sides(self, conn):
        """CIP's own verdict and the agent's reason were in neither the summary nor the hash."""
        ctx = decided_adopt(conn)
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                          rationale="the client wants no third-party auth code")
        first = q.lock_architecture(conn, project_id=ctx.pid)
        s = first["summary_markdown"]
        assert "    CIP computed: ADOPT of pypi:oauth-library" in s
        assert "    agent's reason: the client wants no third-party auth code" in s
        d = first["human_approval_items"]
        assert {"llm_override", "build_over_acceptable_candidate"} <= {f["code"] for f in d}

    def test_coverage_says_where_a_cip_executed_pass_ran(self, cli_db, capsys):
        ctx = locked_build(cli_db)
        tid = implemented(cli_db, ctx)
        out = q.record_executed_verification(cli_db, build_task_id=tid, verification={
            **BOOKINGS_PASS, "evidence_ref": "repo:/somewhere/else", "worktree_dirty": False})
        assert "error" not in out, out
        row = next(r for r in q.coverage(cli_db, project_id=ctx.pid)["by_requirement"]
                   if r["slug"] == "todays-bookings")
        assert row["evidence"][0]["evidence_ref"] == "repo:/somewhere/else"
        assert cli.main(["pdr-coverage", ctx.pid]) == 0
        printed = capsys.readouterr().out
        assert "in repo:/somewhere/else" in printed and "pytest tests/test_bookings.py" in printed
