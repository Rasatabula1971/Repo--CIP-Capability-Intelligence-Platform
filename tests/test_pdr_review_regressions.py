"""
Regression tests for the independent adversarial review of the v1.1 gap
closure. Real tools, real Postgres.

The reviewer was given the code and asked to get round it. Each class
below is one way it did, written as the attack and then the outcome the
workflow now gives. The pure-logic halves are in
test_pdr_core_hardening.py.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys

import pytest

from cip_composer import cli
from db.connection import connect
from mcp_server import pdr_approvals
from mcp_server import pdr_queries as q
from mcp_server import server as mcp_server_module
from tests.pdr_helpers import (
    LICENSE_CONSTRAINT,
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
BOOKINGS_FAIL = {"outcome": "fail", "req_id": "todays-bookings", "criterion_ordinal": 1}


def audit_count(conn, event_type: str) -> int:
    return scalar(conn, "SELECT count(*) FROM audit_event WHERE event_type = %s", (event_type,))


def task_status(conn, task_id: str) -> str:
    return scalar(conn, "SELECT status FROM build_task WHERE id = %s", (task_id,))


def verified_task(conn):
    """A bespoke task taken all the way to verified on real evidence."""
    ctx = locked_build(conn)
    tid = bespoke_task(conn, ctx)["build_task_id"]
    q.record_build_progress(conn, build_task_id=tid, status="in_progress")
    q.record_build_progress(conn, build_task_id=tid, status="implemented")
    out = q.record_build_progress(conn, build_task_id=tid, status="verified",
                                  verification=BOOKINGS_PASS)
    assert out["status"] == "verified", out
    return ctx, tid


@pytest.fixture
def cli_db(monkeypatch, conn):
    monkeypatch.setattr(cli, "connect", lambda: connect(for_tests=True))
    return conn


# ---------------------------------------------------------------------------
# 1. Narrowing the mandatory sweep until it finds nothing
# ---------------------------------------------------------------------------

class TestSweepCannotBeNarrowed:

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_filter_is_no_filter(self, conn, blank):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id, ecosystem=blank, runtime=blank)
        assert res["counts_as_mandatory_sweep"] and res["candidate_count"] == 1
        assert res["filters"] == {}

    @pytest.mark.parametrize("field", ["ecosystem", "capability_kind", "component_kind",
                                       "runtime", "cost_tier"])
    def test_an_empty_string_filter_is_no_filter(self, conn, field):
        """
        `ecosystem=""` was not None, so it filtered on ecosystem = '' and
        matched nothing — while still being logged as the unfiltered sweep.
        """
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id, **{field: ""})
        assert res["counts_as_mandatory_sweep"] and res["candidate_count"] == 1
        assert res["filters"] == {}
        assert scalar(conn, "SELECT filters::text FROM requirement_search") == "{}"

    def test_whitespace_query_is_no_query(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id, search_query="   ")
        assert res["counts_as_mandatory_sweep"] and res["candidate_count"] == 1

    @pytest.mark.parametrize("limit", [0, 1, -1, None, "3"])
    def test_limit_cannot_shrink_the_sweep(self, conn, limit):
        """
        limit=1 returned only the top-scored hit per term. With a popular
        GPL library ranked first, the MIT one that fits was never
        evaluated and CIP's own rules said BUILD.
        """
        make_capability(conn, name="oauth-gpl-popular", display="OAuth GPL Popular",
                        license="GPL-3.0", intrinsic=0.95)
        _, mit = make_capability(conn, name="oauth-mit-solid", display="OAuth MIT Solid",
                                 license="MIT", intrinsic=0.80)
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id, limit=limit)
        assert res["counts_as_mandatory_sweep"] and res["candidate_count"] == 2
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] == "ADOPT" and d["chosen_capability_version_id"] == mit

    def test_a_real_filter_is_extra_evidence_not_the_sweep(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        res = q.search_for_requirement(conn, req_id=ctx.need_id, ecosystem="npm")
        assert not res["counts_as_mandatory_sweep"] and res["filters"] == {"ecosystem": "npm"}
        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and refused["gate"] == "search_before_decision"


# ---------------------------------------------------------------------------
# 2. Reusing an approval for something the human did not see
# ---------------------------------------------------------------------------

STRIPE = {"name": "Stripe", "category": "payment", "purpose": "billing",
          "cost": "usage", "data_leaves_infra": True}
OPENAI = {"name": "OpenAI", "category": "ai", "purpose": "summaries",
          "cost": "usage", "data_leaves_infra": True}


class TestApprovalsAreTiedToHistory:

    def test_an_old_lock_approval_cannot_be_replayed(self, conn):
        """
        v1 approved with Stripe and OpenAI. v2 approved with Stripe only —
        the human took OpenAI out. Asking for the v1 inputs again matched
        the old approved request by hash and wrote v3 with OpenAI back in,
        with no human involved.
        """
        ctx = decided_build(conn)
        v1 = lock(conn, ctx.pid, approved_providers=[STRIPE, OPENAI])
        v2 = lock(conn, ctx.pid, approved_providers=[STRIPE])
        assert (v1["version"], v2["version"]) == (1, 2)
        approvals = scalar(conn, "SELECT count(*) FROM approval")

        again = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[STRIPE, OPENAI])
        assert again["status"] == "awaiting_approval"
        assert again["payload_hash"] != v1["payload_hash"]      # it replaces v2, not nothing
        assert scalar(conn, "SELECT count(*) FROM approval") == approvals
        assert scalar(conn, "SELECT max(version) FROM architecture_lock") == 2

    def test_the_old_request_id_cannot_be_replayed_either(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[STRIPE, OPENAI])
        approve(conn, first["approval_request_id"])
        q.lock_architecture(conn, project_id=ctx.pid,
                            approval_request_id=first["approval_request_id"])
        lock(conn, ctx.pid, approved_providers=[STRIPE])
        replay = q.lock_architecture(conn, project_id=ctx.pid,
                                     approval_request_id=first["approval_request_id"])
        assert replay["refused"] and "can no longer be written" in replay["reason"]
        assert scalar(conn, "SELECT max(version) FROM architecture_lock") == 2

    def test_a_request_overtaken_by_another_lock_cannot_be_approved(self, conn):
        """
        Request X was made against v1 and shows a diff from v1. If v2 is
        written first, approving X would approve a diff that is no longer
        what would happen.
        """
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        x = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["x"])
        assert x["status"] == "awaiting_approval"
        lock(conn, ctx.pid, unresolved_risks=["y"])                       # v2

        res = pdr_approvals.record_approval(
            conn, request_id=x["approval_request_id"], outcome="approved", approver="ricky@desk")
        assert "obsolete" in res["error"] and "active lock is now v2" in res["error"]
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["obsolete_requests"] == 1 and st["pending_approvals"] == []

    def test_an_approval_writes_one_lock(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        approve(conn, first["approval_request_id"])
        a = q.lock_architecture(conn, project_id=ctx.pid,
                                approval_request_id=first["approval_request_id"])
        b = q.lock_architecture(conn, project_id=ctx.pid,
                                approval_request_id=first["approval_request_id"])
        assert a["version"] == 1 and b["version"] == 1 and b["unchanged"] is True
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 1

    def test_the_database_allows_one_lock_per_approval(self, conn):
        """The backstop under the rule: `approval_id` is unique on the lock table."""
        import psycopg
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO architecture_lock
                        (project_id, version, status, approved_by, decisions,
                         rejected_alternatives, approved_providers, cost_commitments,
                         unresolved_risks, source_lock_hash, approval_id, payload_hash,
                         requirements_hash, project_decisions, scope)
                    SELECT project_id, version + 1, 'superseded', approved_by, decisions,
                           rejected_alternatives, approved_providers, cost_commitments,
                           unresolved_risks, source_lock_hash, approval_id, payload_hash,
                           requirements_hash, project_decisions, scope
                    FROM architecture_lock
                    """)
        conn.rollback()

    def test_replay_fails_if_a_decision_changed_after_approval(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        q.record_decision(conn, req_id=ctx.need_id)                       # ADOPT by rule
        first = q.lock_architecture(conn, project_id=ctx.pid)
        approve(conn, first["approval_request_id"])
        q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD",
                          rationale="changed my mind after the human approved")
        res = q.lock_architecture(conn, project_id=ctx.pid,
                                  approval_request_id=first["approval_request_id"])
        assert res["refused"] and "no longer matches" in res["reason"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0

    def test_replay_takes_no_other_inputs_and_only_this_projects_lock_requests(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        mixed = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["r"],
                                    approval_request_id=first["approval_request_id"])
        assert "on its own" in mixed["error"]

        other = scoped(conn, name="other-project")
        conf_id = scalar(conn, "SELECT id::text FROM approval_request WHERE project_id = %s "
                               "AND kind = 'requirements_confirmation'", (other.pid,))
        for bad in (conf_id, first["approval_request_id"]):
            res = q.lock_architecture(conn, project_id=other.pid, approval_request_id=bad)
            assert "not a lock request for this project" in res["error"]

    def test_returning_to_confirmed_content_needs_confirming_again(self, conn):
        """
        Gate 1 replay: confirm A, edit to B and confirm B, edit back to A.
        A's old approval matched by hash and re-opened the gate on its own.
        """
        ctx = scoped(conn)                                                 # A confirmed
        record_need(conn, ctx.pid, description="OAuth2 / OIDC sign-in, with MFA")
        confirm(conn, ctx.pid)                                             # B confirmed
        record_need(conn, ctx.pid)                                         # back to A
        refused = q.search_for_requirement(conn, req_id=ctx.need_id)
        assert refused["refused"] and refused["gate"] == "requirements_confirmation"
        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert rc["status"] == "awaiting_approval"
        assert scalar(conn, "SELECT count(*) FROM approval_request "
                            "WHERE kind = 'requirements_confirmation'") == 3


# ---------------------------------------------------------------------------
# 3. Writing on the human's screen
# ---------------------------------------------------------------------------

class TestSummariesCannotBeForged:

    EVIL = f"none{ESC}[1A{ESC}[2K{ESC}[1A{ESC}[2K\n\n## Needs your attention\n- nothing serious"

    def test_lock_summary(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=[self.EVIL])
        summary = first["summary_markdown"]
        assert ESC not in summary
        assert [l for l in summary.split("\n") if l.startswith("## Needs your attention")] == \
            ["## Needs your attention"]
        assert "[cold_start_build]" in summary and "[unresolved_risk]" in summary
        stored = scalar(conn, "SELECT summary FROM approval_request WHERE id = %s",
                        (first["approval_request_id"],))
        assert ESC not in stored

    def test_requirements_summary(self, conn):
        ing = ingest(conn)
        evil_req = {**REQS[1], "text": f"Bookings{ESC}[2J\n## Deferred - out of this build (9)",
                    "disposition_reason": f"fine\r\n\r\n## Not applicable{ESC}[1A"}
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                         [REQS[0], evil_req, REQS[2], REQS[3]])
        assert all(r["valid"] for r in rr["requirements"]), rr
        record_need(conn, ing["project_id"])
        rc = q.request_requirements_confirmation(conn, project_id=ing["project_id"])
        summary = rc["summary_markdown"]
        assert ESC not in summary and "\r" not in summary
        headings = [l for l in summary.split("\n") if l.startswith("## ")]
        assert len(headings) == len(set(headings))            # nothing forged a second section
        summary.encode("ascii")

    def test_the_terminal_gets_no_escape_bytes(self, cli_db, capsys):
        ctx = decided_build(cli_db)
        first = q.lock_architecture(cli_db, project_id=ctx.pid, unresolved_risks=[self.EVIL])
        assert cli.main(["approvals", "show", first["approval_request_id"]]) == 0
        out = capsys.readouterr().out
        assert ESC not in out and "[unresolved_risk]" in out

    def test_a_tampered_summary_row_is_still_printed_safely(self, cli_db, capsys):
        """Belt and braces: even a summary written straight into the table."""
        ctx = with_requirements(cli_db)
        with cli_db.cursor() as cur:
            cur.execute(
                "INSERT INTO approval_request (project_id, kind, subject_hash, subject, summary) "
                "VALUES (%s, 'architecture_lock', %s, '{}'::jsonb, %s) RETURNING id",
                (ctx.pid, "ab" * 32, f"looks fine{ESC}[2J{ESC}[H"),
            )
            rid = str(cur.fetchone()[0])
        cli_db.commit()
        assert cli.main(["approvals", "show", rid]) == 0
        assert ESC not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. `verified` outliving the evidence under it
# ---------------------------------------------------------------------------

class TestVerifiedDoesNotOutliveItsEvidence:

    def test_a_later_fail_from_the_same_task(self, conn):
        ctx, tid = verified_task(conn)
        out = q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_FAIL)
        assert out["status"] == "implemented"
        assert task_status(conn, tid) == "implemented"                    # stored, not just read
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 0

    def test_a_fail_recorded_by_another_task(self, conn):
        ctx, t1 = verified_task(conn)
        t2 = bespoke_task(conn, ctx, title="Second")["build_task_id"]
        q.record_build_progress(conn, build_task_id=t2, verification=BOOKINGS_FAIL)
        assert task_status(conn, t1) == "implemented"
        tasks = {t["title"]: t["status"] for t in q.pdr_status(conn, project_id=ctx.pid)["tasks"]}
        assert tasks["Bookings screen"] == "implemented"

    def test_a_criterion_added_later(self, conn):
        ctx, tid = verified_task(conn)
        more = {**REQS[1], "acceptance_criteria": ["Today's bookings are listed in time order",
                                                   "Cancelled bookings are hidden"]}
        record_reqs(conn, ctx.pid, ctx.sid, [more])
        confirm(conn, ctx.pid)
        lock(conn, ctx.pid)
        assert task_status(conn, tid) == "implemented"
        row = next(r for r in q.coverage(conn, project_id=ctx.pid)["by_requirement"]
                   if r["slug"] == "todays-bookings")
        assert (row["criteria_passed"], row["criteria_total"]) == (1, 2)
        assert row["verification_outcome"] == "partial"

    def test_a_criterion_rewritten_takes_its_evidence_with_it(self, conn):
        ctx, tid = verified_task(conn)
        rewritten = {**REQS[1], "acceptance_criteria": ["Bookings are listed newest first"]}
        record_reqs(conn, ctx.pid, ctx.sid, [rewritten])
        confirm(conn, ctx.pid)
        lock(conn, ctx.pid)
        assert task_status(conn, tid) == "implemented"
        refused = q.record_build_progress(conn, build_task_id=tid, status="verified")
        assert refused["refused"] and "no verification recorded" in refused["reason"]

    def test_status_reads_the_evidence_not_the_flag(self, conn):
        """Even if the stored flag is wrong, nothing reports it as verified."""
        ctx, tid = verified_task(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM requirement_verification")
        conn.commit()
        assert task_status(conn, tid) == "verified"                       # the stale flag
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["tasks"][0]["status"] == "implemented" and st["phase"] == "building"
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 0

    def test_an_abandoned_task_is_closed(self, conn):
        """
        An abandoned task could still take verification reports, so a pass
        could be filed against work that had been thrown away.
        """
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        assert q.record_build_progress(conn, build_task_id=tid,
                                       status="abandoned")["status"] == "abandoned"
        out = q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_PASS)
        assert out["refused"] and "abandoned" in out["reason"]
        executed = q.record_executed_verification(
            conn, build_task_id=tid, verification=BOOKINGS_PASS)
        assert "abandoned" in executed["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        assert "error" in q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        # Abandoning it again is harmless.
        assert q.record_build_progress(conn, build_task_id=tid,
                                       status="abandoned")["status"] == "abandoned"

    def test_nothing_is_recorded_against_a_stale_lock(self, conn):
        """
        Rewrite the criterion (lock goes stale, scope unconfirmed), then
        report a pass against the new wording before any human has seen it.
        """
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        easier = {**REQS[1], "acceptance_criteria": ["The page loads"]}
        record_reqs(conn, ctx.pid, ctx.sid, [easier])

        out = q.record_build_progress(conn, build_task_id=tid, status="verified",
                                      verification=BOOKINGS_PASS)
        assert out["refused"] and "stale" in out["reason"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        assert task_status(conn, tid) == "implemented"
        # The human's own `cip-composer verify` is held to the same rule.
        executed = q.record_executed_verification(
            conn, build_task_id=tid, verification=BOOKINGS_PASS)
        assert "stale" in executed["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        # Walking away from the task is still allowed.
        assert q.record_build_progress(conn, build_task_id=tid,
                                       status="abandoned")["status"] == "abandoned"


# ---------------------------------------------------------------------------
# 5. "Latest wins" inside one call
# ---------------------------------------------------------------------------

class TestLatestWinsIsOrdered:

    @pytest.mark.parametrize("order,expect", [
        ([BOOKINGS_PASS, BOOKINGS_FAIL], "implemented"),
        ([BOOKINGS_FAIL, BOOKINGS_PASS], "verified"),
    ])
    def test_the_last_report_in_a_call_wins(self, conn, order, expect):
        """
        Both rows of one call shared a transaction timestamp, so which was
        'latest' was arbitrary: a pass followed by a fail could verify.
        """
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        for _ in range(4):                                # the same answer every time
            q.record_build_progress(conn, build_task_id=tid, verification=order)
            out = q.record_build_progress(conn, build_task_id=tid, status="verified")
            got = out["status"] if "refused" not in out else task_status(conn, tid)
            assert got == expect
            if got == "verified":
                q.record_build_progress(conn, build_task_id=tid, status="implemented")


# ---------------------------------------------------------------------------
# 6. Lock check, through the tool
# ---------------------------------------------------------------------------

class TestLockCheckThroughTheTool:

    def test_project_decisions_are_checked(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid, project_decisions=[
            {"key": "database", "category": "database", "choice": "Postgres"}])
        same = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "project_decision_changes": [{"key": "database", "choice": "postgres"}]})
        swap = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "description": "use Mongo for bookings",
            "project_decision_changes": [{"key": "database", "choice": "MongoDB"}]})
        assert same["verdict"] == "allowed"
        assert swap["verdict"] == "needs_approval" and "is 'Postgres'" in swap["reason"]

    def test_using_a_provider_the_lock_does_not_hold(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid, approved_providers=[STRIPE])
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "provider_changes": [{"name": "OpenAI", "action": "use"}]})
        assert out["verdict"] == "needs_approval"

    @pytest.mark.parametrize("bad", ["nul\x00here", "lone \ud800 surrogate"])
    def test_unstorable_text_is_blocked_and_still_logged(self, conn, bad):
        """This used to raise before the audit row was written."""
        ctx = locked_build(conn)
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "description": bad, "dependency_adds": [{"ecosystem": "pypi", "name": "requests"}]})
        assert out["verdict"] == "blocked" and "could not be read" in out["reason"]
        assert scalar(conn, "SELECT count(*) FROM audit_event WHERE event_type = 'lock_check' "
                            "AND payload->>'verdict' = 'blocked'") == 1

    def test_unknown_keys_escalate_and_are_logged(self, conn):
        ctx = locked_build(conn)
        out = q.check_against_lock(conn, project_id=ctx.pid, proposed_change={
            "new_services": ["OpenAI"], "dependency_adds": []})
        assert out["verdict"] in ("needs_approval", "blocked")
        assert audit_count(conn, "lock_check") == 1


# ---------------------------------------------------------------------------
# 7. Paths that permit everything
# ---------------------------------------------------------------------------

class TestPermittedPaths:

    @pytest.mark.parametrize("paths", [["**"], ["*"], ["**/x.py"], ["."], [""], "src/",
                                       [5, "src/"], ["src/../.."]])
    def test_a_task_cannot_permit_itself_the_repository(self, conn, paths):
        ctx = locked_build(conn)
        out = bespoke_task(conn, ctx, permitted_paths=paths)
        assert out["refused"] and out.get("build_task_id") is None
        assert scalar(conn, "SELECT count(*) FROM build_task") == 0


# ---------------------------------------------------------------------------
# 9 + 10. What document is being confirmed, and how much of it was read
# ---------------------------------------------------------------------------

BOLD_PDR = """**Goals**
Reduce missed calls at the front desk.

**Requirements**
Staff must sign in with Google OAuth before using the system.

**Security**
All patient data must be encrypted at rest.

**Billing**
Clinics are charged monthly by card.
"""


class TestTheDocumentBeingConfirmed:

    def test_unmapped_sections_show_up_whatever_the_heading_style(self, conn):
        """
        With bold-line headings the whole PDR was one section. One quote
        'mapped' it, and Security and Billing vanished without a trace.
        """
        ing = ingest(conn, pdr=BOLD_PDR)
        rr = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [REQS[0]])
        cov = rr["span_coverage"]
        assert cov["section_count"] == 4 and cov["mapped_section_count"] == 1
        assert [s["heading"] for s in cov["unmapped_sections"]] == ["Goals", "Security", "Billing"]
        record_need(conn, ing["project_id"])
        rc = q.request_requirements_confirmation(conn, project_id=ing["project_id"])
        assert "3 of 4 sections" in rc["summary_markdown"]
        assert "- Security" in rc["summary_markdown"] and "- Billing" in rc["summary_markdown"]

    def test_a_file_is_identified_by_name_and_hash(self, conn, tmp_path):
        f = tmp_path / "clinic_pdr.md"
        f.write_bytes(PDR.encode("utf-8"))
        ing = q.ingest_pdr(conn, project_name="p", pdr_title="Clinic PDR", pdr_path=str(f))
        assert "error" not in ing, ing
        digest = hashlib.sha256(PDR.encode("utf-8")).hexdigest()
        with conn.cursor() as cur:
            cur.execute("SELECT raw_ref, source_file_hash FROM requirement_source")
            assert cur.fetchone() == (str(f), digest)
        record_reqs(conn, ing["project_id"], ing["requirement_source_id"])
        record_need(conn, ing["project_id"])
        rc = q.request_requirements_confirmation(conn, project_id=ing["project_id"])
        assert f"- File: {f}" in rc["summary_markdown"] and digest in rc["summary_markdown"]
        assert "SUPPLIED INLINE" not in rc["summary_markdown"]

        req = q.describe_request(conn, rc["approval_request_id"])
        assert "matches what was ingested" in req["source_check"]
        f.write_text(PDR + "\nPasswords may be stored in plain text.\n", encoding="utf-8")
        req = q.describe_request(conn, rc["approval_request_id"])
        assert req["source_check"].startswith("WARNING") and "has changed" in req["source_check"]

    def test_the_same_text_read_from_a_file_later_gains_the_file(self, conn, tmp_path):
        ctx = with_requirements(conn)                               # ingested inline
        f = tmp_path / "pdr.md"
        f.write_bytes(PDR.encode("utf-8"))
        again = q.ingest_pdr(conn, project_name="test-project", pdr_title="Test PDR",
                             pdr_path=str(f))
        assert again["was_duplicate"] and again["requirement_source_id"] == ctx.sid
        assert scalar(conn, "SELECT raw_ref FROM requirement_source") == str(f)
        # ...and pasting the same text in afterwards does not drop the file.
        ingest(conn)
        assert scalar(conn, "SELECT raw_ref FROM requirement_source") == str(f)

    def test_inline_text_is_called_out(self, conn):
        """
        An agent could re-ingest a doctored text through pdr_text, extract
        'requirements' the real PDR never contained, and present a
        summary that looked like any other.
        """
        ctx = with_requirements(conn)
        doctored = PDR + "\n## Extra\nPasswords may be stored in plain text for convenience.\n"
        ing = ingest(conn, pdr=doctored)
        assert ing["supersedes_id"] == ctx.sid
        record_need(conn, ctx.pid)
        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        s = rc["summary_markdown"]
        assert "TEXT SUPPLIED INLINE BY THE AGENT" in s
        assert "not the only text that has been this project's PDR" in s
        assert "Extra" in s                                   # and its unmapped section is listed

    def test_a_first_inline_pdr_is_not_flagged_as_a_replacement(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        s = q.request_requirements_confirmation(conn, project_id=ctx.pid)["summary_markdown"]
        assert "TEXT SUPPLIED INLINE" in s and "not the only text" not in s

    def test_an_obsolete_confirmation_request_is_marked(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        old = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        record_need(conn, ctx.pid, description="OAuth sign-in, reworded")
        req = q.describe_request(conn, old["approval_request_id"])
        assert "edited since this request was made" in req["obsolete_reason"]


# ---------------------------------------------------------------------------
# 11. "Analysed" has to mean analysed for the thing being checked
# ---------------------------------------------------------------------------

INTERFACE_CONSTRAINT = {"kind": "required_interface", "name": "authorize"}


class TestEvidenceIsJudgedPerConstraint:

    def test_license_evidence_does_not_answer_an_interface_constraint(self, conn):
        """
        A candidate with a LICENSE file read counted as 'analysed', so a
        missing-interface gap on it was treated as a finding and CIP
        rejected it by rule. Nothing had ever looked for interfaces.
        """
        _, cv = make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[INTERFACE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "insufficient" and cand["missing_evidence"] == ["interfaces"]
        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "holds no analysed evidence" in refused["reason"]
        d = q.record_decision(conn, req_id=ctx.need_id, verdict="ADOPT",
                              chosen_capability_version_id=cv, rationale="read the source")
        assert d["decided_by"] == "llm_judgment"
        assert d["flags"] == ["unanalysed_candidate", "judgment:insufficient_evidence"]

    def test_the_same_candidate_is_sufficient_for_a_license_constraint(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient" and cand["missing_evidence"] == []
        assert q.record_decision(conn, req_id=ctx.need_id)["decided_by"] == "cip_rules"

    def test_both_constraints_need_both_kinds_of_evidence(self, conn):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT, INTERFACE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["missing_evidence"] == ["interfaces"]


# ---------------------------------------------------------------------------
# 12. Slugs
# ---------------------------------------------------------------------------

class TestSlugCollisions:

    def test_a_derived_slug_does_not_overwrite_another_requirement(self, conn):
        """
        Two different requirements whose text slugifies the same: the
        second silently replaced the first, quote and criteria included.
        """
        ing = ingest(conn)
        first = {"text": "Staff sign in", "acceptance_criteria": ["a"],
                 "quote": "Staff must sign in with Google OAuth before using the system."}
        clash = {"text": "Staff: sign in!", "acceptance_criteria": ["b"],
                 "quote": "The receptionist can see today's bookings on one screen.",
                 "disposition": "bespoke", "disposition_reason": "x"}
        a = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [first])
        b = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [clash])
        assert a["requirements"][0]["valid"]
        assert not b["requirements"][0]["valid"]
        assert any("slug" in e for e in b["requirements"][0]["errors"])
        assert scalar(conn, "SELECT count(*) FROM project_requirement") == 1
        assert "Google OAuth" in scalar(conn, "SELECT source_span->>'quote' FROM project_requirement")

    def test_an_explicit_slug_updates(self, conn):
        ctx = with_requirements(conn)
        upd = {**REQS[1], "text": "Receptionist sees the day's bookings"}
        r = record_reqs(conn, ctx.pid, ctx.sid, [upd])
        assert r["requirements"][0]["valid"]
        assert r["requirements"][0]["req_id"] == ctx.reqs["todays-bookings"]

    def test_a_slug_twice_in_one_call_is_refused(self, conn):
        ing = ingest(conn)
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                        [REQS[0], {**REQS[1], "slug": REQS[0]["slug"]}])
        first, second = r["requirements"]
        assert first["valid"] and not second["valid"]
        assert "is the same as item 0 in this call" in second["errors"][0]
        # The first one is what was stored; the second did not replace it.
        assert scalar(conn, "SELECT count(*) FROM project_requirement") == 1
        assert "Google OAuth" in scalar(conn, "SELECT source_span->>'quote' FROM project_requirement")


# ---------------------------------------------------------------------------
# 13. Resuming in a session that has none of the inputs
# ---------------------------------------------------------------------------

class TestResumeAcrossTheLockApproval:

    INPUTS = dict(
        approved_providers=[STRIPE],
        cost_commitments=[{"item": "Hosting", "kind": "recurring", "amount": "5",
                           "currency": "USD", "period": "month"}],
        project_decisions=[{"key": "database", "category": "database", "choice": "Postgres"}],
        unresolved_risks=["vendor lock-in"],
        architecture_doc_text="# Architecture\nA monolith.\n",
    )

    def test_status_carries_everything_needed_to_finish(self, conn):
        """
        The second lock call had to repeat the inputs byte for byte. A new
        session did not have them, and pdr_status did not return them.
        """
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, **self.INPUTS)
        waiting = q.pdr_status(conn, project_name="test-project")
        assert waiting["phase"] == "awaiting_lock_approval"
        assert waiting["approved_lock_request"] is None
        approve(conn, first["approval_request_id"])

        # --- a new session: only the project name ---
        st = q.pdr_status(conn, project_name="test-project")
        rid = st["approved_lock_request"]["approval_request_id"]
        assert st["phase"] == "ready_to_lock" and rid == first["approval_request_id"]
        assert f"approval_request_id='{rid}'" in st["next_actions"][0]
        written = q.lock_architecture(conn, project_id=st["project_id"], approval_request_id=rid)
        assert written["version"] == 1 and written["payload_hash"] == first["payload_hash"]

        after = q.pdr_status(conn, project_name="test-project")
        assert after["phase"] == "building" and after["approved_lock_request"] is None
        frozen = after["lock"]
        assert frozen["approved_providers"][0]["name"] == "Stripe"
        assert frozen["cost_commitments"][0]["item"] == "Hosting"
        assert frozen["project_decisions"][0]["choice"] == "Postgres"
        assert frozen["unresolved_risks"] == ["vendor lock-in"]
        assert frozen["decisions"][0]["verdict"] == "BUILD"

    def test_the_lock_written_by_replay_keeps_its_document_reference(self, conn, tmp_path):
        arch = tmp_path / "architecture.md"
        arch.write_text("# Architecture\nA monolith.\n", encoding="utf-8")
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, architecture_doc_path=str(arch))
        approve(conn, first["approval_request_id"])
        q.lock_architecture(conn, project_id=ctx.pid,
                            approval_request_id=first["approval_request_id"])
        with conn.cursor() as cur:
            cur.execute("SELECT architecture_doc_ref, architecture_doc_hash IS NOT NULL "
                        "FROM architecture_lock")
            assert cur.fetchone() == (str(arch), True)
        assert q.pdr_status(conn, project_id=ctx.pid)["lock"]["architecture_doc_ref"] == str(arch)

    def test_an_approved_relock_is_announced_while_the_old_lock_still_works(self, conn):
        ctx = decided_build(conn)
        lock(conn, ctx.pid)
        second = q.lock_architecture(conn, project_id=ctx.pid, unresolved_risks=["new"])
        approve(conn, second["approval_request_id"])
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["phase"] == "building"
        assert second["approval_request_id"] in st["next_actions"][0]


# ---------------------------------------------------------------------------
# 14. Refusals leave a trace
# ---------------------------------------------------------------------------

class TestRefusalsAreAudited:

    def test_every_gate_refusal_writes_an_audit_event(self, conn):
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid)["need_id"]
        n0 = audit_count(conn, "gate_refused")

        assert q.search_for_requirement(conn, req_id=need)["refused"]        # unconfirmed scope
        assert q.record_decision(conn, req_id=need)["refused"]
        assert q.lock_architecture(conn, project_id=ctx.pid)["refused"]
        assert audit_count(conn, "gate_refused") == n0 + 3

        confirm(conn, ctx.pid)
        assert q.record_decision(conn, req_id=need)["refused"]                # no sweep yet
        q.search_for_requirement(conn, req_id=need)
        q.record_decision(conn, req_id=need)
        lock(conn, ctx.pid)
        ctx.need_id = need
        n1 = audit_count(conn, "gate_refused")
        assert bespoke_task(conn, ctx, req_ids=["dark-mode"])["refused"]      # deferred requirement
        assert audit_count(conn, "task_refused") == 1
        tid = bespoke_task(conn, ctx)["build_task_id"]
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        assert q.record_build_progress(conn, build_task_id=tid, status="verified")["refused"]
        # Everything refused about a task is one kind of event.
        assert audit_count(conn, "gate_refused") == n1
        assert audit_count(conn, "task_refused") == 2

        with conn.cursor() as cur:
            cur.execute("SELECT payload->>'tool' FROM audit_event "
                        "WHERE event_type = 'gate_refused' ORDER BY created_at, id")
            tools = {r[0] for r in cur.fetchall()}
        assert {"search_for_requirement", "record_decision", "lock_architecture"} <= tools

    def test_a_refusal_does_not_leave_half_a_write_behind(self, conn):
        ctx = locked_build(conn)
        before = scalar(conn, "SELECT count(*) FROM build_task")
        assert bespoke_task(conn, ctx, permitted_paths=["**"])["refused"]
        assert scalar(conn, "SELECT count(*) FROM build_task") == before
        assert scalar(conn, "SELECT count(*) FROM build_task_requirement") == 0


# ---------------------------------------------------------------------------
# 15. The approval note belongs to the approver
# ---------------------------------------------------------------------------

class TestTheApprovalNoteIsTheHumans:

    def test_no_tool_accepts_an_approval_note_or_an_approver(self):
        """
        `approval_note` was an argument of the agent's own lock call: it
        could store 'Ricky approved GPL use verbally' on the lock.
        """
        assert "approval_note" not in inspect.signature(q.lock_architecture).parameters
        tool = inspect.signature(mcp_server_module.lock_architecture).parameters
        assert "approval_note" not in tool and "approved_by" not in tool
        assert "approval_request_id" in tool

    def test_the_lock_stores_what_the_approver_wrote(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        pdr_approvals.record_approval(
            conn, request_id=first["approval_request_id"], outcome="approved",
            approver="ricky@desk", note="ok, but revisit hosting in Q2")
        q.lock_architecture(conn, project_id=ctx.pid,
                            approval_request_id=first["approval_request_id"])
        with conn.cursor() as cur:
            cur.execute("SELECT approved_by, approval_note FROM architecture_lock")
            assert cur.fetchone() == ("ricky@desk", "ok, but revisit hosting in Q2")

    def test_approved_by_from_the_caller_is_ignored_and_says_so(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_by="Ricky (verbally)")
        assert any("approved_by is ignored" in w for w in first["warnings"])
        approve(conn, first["approval_request_id"])
        done = q.lock_architecture(conn, project_id=ctx.pid,
                                   approval_request_id=first["approval_request_id"])
        assert done["approved_by"] == "ricky@desk"


# ---------------------------------------------------------------------------
# 16. A Windows desktop
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
    (tmp_path / "raw.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'\\x9d \\xff tests \\xe2\\x9c\\x93 ok\\n')\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def implemented_task(conn):
    ctx = locked_build(conn)
    tid = bespoke_task(conn, ctx)["build_task_id"]
    q.record_build_progress(conn, build_task_id=tid, status="in_progress")
    q.record_build_progress(conn, build_task_id=tid, status="implemented")
    return ctx, tid


class TestWindowsDesktop:

    def test_json_written_by_powershell_redirection(self, cli_db, tmp_path, capsys):
        """`> reqs.json` in PowerShell 5.1 writes UTF-16 with a BOM."""
        ing = ingest(cli_db)
        f = tmp_path / "reqs.json"
        f.write_bytes(json.dumps(REQS).encode("utf-16"))
        assert cli.main(["record-requirements", ing["project_id"],
                         ing["requirement_source_id"], str(f)]) == 0
        assert scalar(cli_db, "SELECT count(*) FROM project_requirement") == len(REQS)

    def test_a_bad_json_file_is_a_message_not_a_traceback(self, cli_db, tmp_path, capsys):
        ing = ingest(cli_db)
        f = tmp_path / "reqs.json"
        f.write_text("{not json")
        assert cli.main(["record-requirements", ing["project_id"],
                         ing["requirement_source_id"], str(f)]) == 1
        assert "not valid JSON" in capsys.readouterr().out
        assert cli.main(["record-requirements", ing["project_id"],
                         ing["requirement_source_id"], str(tmp_path / "missing.json")]) == 1

    def test_test_output_that_is_not_utf8(self, cli_db, target_repo, capsys):
        """
        Output was decoded as text before hashing. A byte that is not
        valid in the console code page raised, and the run was lost.
        """
        ctx, tid = implemented_task(cli_db)
        rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                       "--", sys.executable, "raw.py"])
        assert rc == 0 and "PASS" in capsys.readouterr().out
        raw = b"\x9d \xff tests \xe2\x9c\x93 ok\n"
        assert scalar(cli_db, "SELECT output_digest FROM requirement_verification") == \
            hashlib.sha256(raw).hexdigest()

    def test_a_crash_exit_code_fits(self, conn):
        ctx, tid = implemented_task(conn)
        res = q.record_executed_verification(conn, build_task_id=tid, verification={
            "outcome": "fail", "req_id": "todays-bookings", "criterion_ordinal": 1,
            "commit_sha": "abc1234", "command": "vitest", "test_ref": "x",
            "exit_code": 0xC0000005})
        assert "error" not in res, res
        assert scalar(conn, "SELECT exit_code FROM requirement_verification") == 3221225477

    def test_a_program_that_is_not_on_path(self, cli_db, target_repo, capsys):
        ctx, tid = implemented_task(cli_db)
        rc = cli.main(["verify", "--task", tid, "--req", "todays-bookings", "--criterion", "1",
                       "--", "npm-does-not-exist", "test"])
        assert rc == 1 and "could not find the program" in capsys.readouterr().out
        assert scalar(cli_db, "SELECT count(*) FROM requirement_verification") == 0

    def test_the_tool_refuses_a_relative_path_and_the_cli_resolves_one(self, cli_db, tmp_path,
                                                                        monkeypatch):
        """
        The MCP server runs in CIP's own folder. 'docs/pdr.md' resolved
        there, not in the project the agent was working on.
        """
        res = q.ingest_pdr(cli_db, project_name="p", pdr_title="t", pdr_path="docs/pdr.md")
        assert "must be absolute" in res["error"]
        (tmp_path / "pdr.md").write_text(PDR, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert cli.main(["ingest-pdr", "pdr.md", "--project-name", "from-cli"]) == 0
        assert scalar(cli_db, "SELECT raw_ref FROM requirement_source") == str(
            (tmp_path / "pdr.md").resolve())

    def test_a_utf16_pdr_file_is_read(self, conn, tmp_path):
        f = tmp_path / "pdr.txt"
        f.write_bytes(PDR.encode("utf-16"))
        ing = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(f))
        assert "error" not in ing
        assert scalar(conn, "SELECT raw_text FROM requirement_source").startswith("# Clinic")

    def test_a_terminal_that_is_not_one(self, cli_db, monkeypatch, capsys):
        """On Windows the NUL device says it is a tty; reading it hits end-of-input."""
        ctx = with_requirements(cli_db)
        record_need(cli_db, ctx.pid)
        rc_req = q.request_requirements_confirmation(cli_db, project_id=ctx.pid)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

        def eof(prompt=""):
            raise EOFError
        monkeypatch.setattr("builtins.input", eof)
        assert cli.main(["approvals", "approve", rc_req["approval_request_id"]]) == 3
        assert "Nothing was recorded" in capsys.readouterr().out
        assert scalar(cli_db, "SELECT count(*) FROM approval") == 0

    def test_the_approver_name_does_not_come_from_the_environment(self, monkeypatch):
        """getpass.getuser() reads LOGNAME / USER / USERNAME before asking the OS."""
        for var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
            monkeypatch.setenv(var, "the-ceo")
        assert not cli._human_identity().startswith("the-ceo@")

    def test_output_survives_a_narrow_console(self, cli_db):
        """A cp1252 console could not print the summary and the command crashed."""
        ctx = decided_build(cli_db)
        env = {**__import__("os").environ, "PYTHONIOENCODING": "cp1252",
               "DATABASE_URL": __import__("os").environ["TEST_DATABASE_URL"]}
        proc = subprocess.run(
            [sys.executable, "-m", "cip_composer.cli", "pdr-lock", ctx.pid],
            capture_output=True, env=env, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert b"Awaiting approval" in proc.stdout


# ---------------------------------------------------------------------------
# 17. Going back to an earlier PDR
# ---------------------------------------------------------------------------

class TestRevertingThePdr:

    def test_an_earlier_text_can_be_made_current_again(self, conn):
        """
        Re-ingesting text A after B reported 'duplicate' and left B
        current. There was no way back.
        """
        ctx = with_requirements(conn)
        b = ingest(conn, pdr=PDR + "\n## New\nSomething new is required here.\n")
        assert b["supersedes_id"] == ctx.sid and b["is_current"]

        back = ingest(conn)
        assert back["requirement_source_id"] == ctx.sid and back["is_current"]
        assert back["was_duplicate"] is True
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["pdr"]["requirement_source_id"] == ctx.sid
        # Requirements are anchored to the current text again and the project can proceed.
        assert all(r["valid"] for r in record_reqs(conn, ctx.pid, ctx.sid)["requirements"])
        record_need(conn, ctx.pid)
        rc = q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert rc["status"] == "awaiting_approval"
        assert "not the only text" in rc["summary_markdown"]      # the human is told it moved


# ---------------------------------------------------------------------------
# 18. Rows that predate v1.1
# ---------------------------------------------------------------------------

class TestLegacyRows:

    def _legacy_pass(self, conn, ctx, tid):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO requirement_verification "
                "(project_requirement_id, build_task_id, outcome, legacy) "
                "VALUES (%s, %s, 'pass', true)",
                (ctx.reqs["todays-bookings"], tid),
            )
        conn.commit()

    def test_a_v1_0_task_with_a_pass_row_can_still_be_deleted(self, conn):
        """
        Deleting the task sets build_task_id NULL on its verification rows.
        That UPDATE re-ran the new pass-evidence CHECK against a v1.0 row
        with no evidence, and the delete failed.
        """
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        self._legacy_pass(conn, ctx, tid)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM build_task WHERE id = %s", (tid,))
        conn.commit()
        assert scalar(conn, "SELECT count(*) FROM requirement_verification "
                            "WHERE build_task_id IS NULL AND legacy") == 1

    def test_a_legacy_pass_never_counts_as_verification(self, conn):
        ctx = locked_build(conn)
        tid = bespoke_task(conn, ctx)["build_task_id"]
        self._legacy_pass(conn, ctx, tid)
        assert q.coverage(conn, project_id=ctx.pid)["progress"]["verified"] == 0
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        assert q.record_build_progress(conn, build_task_id=tid, status="verified")["refused"]

    def test_a_new_pass_without_evidence_is_still_refused_by_the_database(self, conn):
        import psycopg
        ctx = locked_build(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO requirement_verification (project_requirement_id, outcome) "
                    "VALUES (%s, 'pass')", (ctx.reqs["todays-bookings"],))
        conn.rollback()

    def test_the_whole_project_can_be_deleted(self, conn):
        ctx, tid = verified_task(conn)
        self._legacy_pass(conn, ctx, tid)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM project WHERE id = %s", (ctx.pid,))
        conn.commit()
        for table in ("project_requirement", "architecture_lock", "build_task",
                      "requirement_verification", "approval_request", "decision_record"):
            assert scalar(conn, f"SELECT count(*) FROM {table}") == 0, table


# ---------------------------------------------------------------------------
# 19. Bad input is an answer, not an exception
# ---------------------------------------------------------------------------

class TestBadInputIsAnError:

    def test_ingest(self, conn):
        for kwargs in (dict(project_name="p", pdr_title=None, pdr_text=PDR),
                       dict(project_name=None, pdr_title="t", pdr_text=PDR),
                       dict(project_name="p", pdr_title="t", pdr_text=5),
                       dict(project_name="p", pdr_title="t", pdr_text="nul\x00"),
                       dict(project_name="p", pdr_title="t", pdr_text="s \ud800"),
                       dict(project_name="p", pdr_title="t", pdr_path=5),
                       dict(project_name="p", pdr_title="t", pdr_text=PDR, pdr_path="/x.md"),
                       dict(project_name="p", pdr_title="t")):
            assert "error" in q.ingest_pdr(conn, **kwargs), kwargs
        assert scalar(conn, "SELECT count(*) FROM project") == 0

    def test_requirements_and_needs(self, conn):
        ing = ingest(conn)
        pid, sid = ing["project_id"], ing["requirement_source_id"]
        assert "error" in q.record_requirements(conn, project_id=pid, requirement_source_id=sid,
                                                requirements=None)
        assert "error" in record_reqs(conn, pid, sid, [{**REQS[0], "text": "nul\x00"}])
        junk = record_reqs(conn, pid, sid, [None, 5, "x", {"text": 5, "quote": []}])
        assert [r["valid"] for r in junk["requirements"]] == [False] * 4
        assert "error" in q.record_capability_needs(conn, project_id=pid, needs="oauth")
        bad_need = q.record_capability_needs(conn, project_id=pid, needs=[
            {"description": "x", "search_terms": "oauth", "satisfies": 5}])
        assert not bad_need["needs"][0]["valid"]

    def test_lock_inputs(self, conn):
        ctx = decided_build(conn)
        for kwargs in (dict(unresolved_risks=["nul\x00"]),
                       dict(unresolved_risks=["s \ud800"]),
                       dict(cost_commitments=[{"item": "x", "kind": "recurring",
                                               "amount": float("nan")}])):
            assert "error" in q.lock_architecture(conn, project_id=ctx.pid, **kwargs), kwargs
        for kwargs in (dict(approved_providers="Stripe"),
                       dict(unresolved_risks=[5]),
                       dict(project_decisions=[{"key": 5}]),
                       dict(architecture_doc_text=5),
                       dict(architecture_doc_path="relative/arch.md")):
            assert q.lock_architecture(conn, project_id=ctx.pid, **kwargs)["refused"], kwargs
        assert scalar(conn, "SELECT count(*) FROM approval_request "
                            "WHERE kind = 'architecture_lock'") == 0

    def test_tasks_and_verification(self, conn):
        ctx = locked_build(conn)
        assert "error" in bespoke_task(conn, ctx, title=5)
        assert "error" in bespoke_task(conn, ctx, req_ids="todays-bookings")
        # A bare string used to be stored as a list of single characters.
        out = bespoke_task(conn, ctx, prohibited_changes="do not touch the session schema")
        assert "must be a list" in out["error"]
        tid = bespoke_task(conn, ctx)["build_task_id"]
        for bad in ("pass", [None], {**BOOKINGS_PASS, "commit_sha": "unknown"},
                    {**BOOKINGS_PASS, "exit_code": "0"},
                    {**BOOKINGS_PASS, "command": "nul\x00"}):
            assert "error" in q.record_build_progress(conn, build_task_id=tid, verification=bad)
        assert "error" in q.record_build_progress(conn, build_task_id=tid, status=5)
        assert "error" in q.record_build_progress(conn, build_task_id=5, status="planned")
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0

    def test_decision_and_search(self, conn):
        ctx = scoped(conn)
        assert "error" in q.search_for_requirement(conn, req_id=ctx.need_id, search_query="n\x00")
        q.search_for_requirement(conn, req_id=ctx.need_id)
        assert "error" in q.record_decision(conn, req_id=ctx.need_id, verdict=5)
        assert "error" in q.record_decision(conn, req_id=ctx.need_id, rationale=None)
        assert q.record_decision(conn, req_id=ctx.need_id,
                                 chosen_capability_version_id="not-a-uuid")["refused"]
        assert q.search_for_requirement(conn, req_id=None) is None
        assert q.pdr_status(conn, project_id=5) is None
        assert q.coverage(conn, project_id="nope") is None


# ---------------------------------------------------------------------------
# 20. Ids and files
# ---------------------------------------------------------------------------

class TestIdsAndFiles:

    @pytest.mark.parametrize("ref", ["%", "", "________", "abc", "abcde", "zzzzzzzz", None, 5,
                                     "' OR 1=1 --"])
    def test_request_lookup_takes_only_a_hex_prefix(self, conn, ref):
        """
        The prefix lookup was a LIKE, so '%' matched the only pending
        request: `approvals approve %` approved whatever was there.
        """
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        q.request_requirements_confirmation(conn, project_id=ctx.pid)
        assert pdr_approvals.get_request(conn, ref) is None
        assert q.describe_request(conn, ref) is None

    def test_a_real_prefix_still_works(self, conn):
        ctx = with_requirements(conn)
        record_need(conn, ctx.pid)
        rid = q.request_requirements_confirmation(conn, project_id=ctx.pid)["approval_request_id"]
        assert pdr_approvals.get_request(conn, rid[:8])["id"] == rid
        assert pdr_approvals.get_request(conn, rid.upper())["id"] == rid
        assert pdr_approvals.get_request(conn, rid[:6])["id"] == rid
        # Too short to be deliberate, even though it matches exactly one request.
        for n in (1, 3, 5):
            assert pdr_approvals.get_request(conn, rid[:n]) is None

    @pytest.mark.parametrize("name", [".env", "id_rsa", "secrets.json", "notes.pdf", "pdr.md.bak"])
    def test_the_document_reader_only_reads_documents(self, conn, tmp_path, name):
        """
        ingest_pdr(pdr_path=...) would read any file the server could and
        store it where the agent could read it back.
        """
        f = tmp_path / name
        f.write_text("SECRET=1\n")
        res = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(f))
        assert "must be a text document" in res["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_source") == 0

    def test_document_roots_fence_the_reader_in(self, conn, tmp_path, monkeypatch):
        allowed, outside = tmp_path / "allowed", tmp_path / "outside"
        allowed.mkdir(), outside.mkdir()
        (allowed / "pdr.md").write_text(PDR, encoding="utf-8")
        (outside / "pdr.md").write_text(PDR, encoding="utf-8")
        monkeypatch.setenv("CIP_DOCUMENT_ROOTS", str(allowed))
        refused = q.ingest_pdr(conn, project_name="p", pdr_title="t",
                               pdr_path=str(outside / "pdr.md"))
        assert "outside the folders allowed" in refused["error"]
        sneaky = q.ingest_pdr(conn, project_name="p", pdr_title="t",
                              pdr_path=str(allowed / ".." / "outside" / "pdr.md"))
        assert "outside the folders allowed" in sneaky["error"]
        ok = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(allowed / "pdr.md"))
        assert "error" not in ok

    def test_a_file_can_hold_what_an_argument_cannot(self, conn, tmp_path):
        """UTF-16 saved without a byte-order mark decodes as UTF-8 full of NULs."""
        f = tmp_path / "pdr.txt"
        f.write_bytes("Staff must sign in.\n".encode("utf-16-le"))
        res = q.ingest_pdr(conn, project_name="p", pdr_title="t", pdr_path=str(f))
        assert "cannot be stored" in res["error"] and "UTF-8" in res["error"]
        assert scalar(conn, "SELECT count(*) FROM requirement_source") == 0

    def test_an_oversized_or_missing_document(self, conn, tmp_path):
        missing = q.ingest_pdr(conn, project_name="p", pdr_title="t",
                               pdr_path=str(tmp_path / "nope.md"))
        assert "not found" in missing["error"]
        big = tmp_path / "big.md"
        big.write_text("x" * 3_000_000)
        assert "larger than" in q.ingest_pdr(conn, project_name="p", pdr_title="t",
                                             pdr_path=str(big))["error"]

    def test_an_in_scope_constraint_must_be_verifiable(self, conn):
        """
        A constraint requirement with no criteria and no disposition sat
        in scope with nothing that could ever mark it verified.
        """
        ing = ingest(conn)
        bare = {"slug": "permissive-licenses", "kind": "constraint",
                "text": "MIT or Apache dependencies only",
                "quote": "All dependencies must be MIT or Apache licensed.",
                "disposition": "bespoke", "disposition_reason": "checked in CI"}
        r = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [bare])
        assert not r["requirements"][0]["valid"]
        ok = record_reqs(conn, ing["project_id"], ing["requirement_source_id"],
                         [{**bare, "acceptance_criteria": ["The license check passes in CI"]}])
        assert ok["requirements"][0]["valid"]
