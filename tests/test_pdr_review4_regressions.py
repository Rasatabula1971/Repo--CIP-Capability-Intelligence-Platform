"""
Regression tests for the fourth independent review of the PDR-to-Build
slice. Real Postgres, real tools.

The fourth review was narrow: had the third fix pass broken a gate, or left
one of its own fixes half done? About 135 attempts and a 250-step random
walk over the lock state machine opened no gate and wrote no lock without
a human approval for that content. It did find four ways CIP or the human
could still be made to assert something false, eight bounded faults and
eight smaller ones. Each class here is one finding, named for what must
now hold. The pure-logic side is in test_pdr_core_hardening.py.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from cip_composer import cli
from core.pdr import lock as lock_rules
from core.pdr import requirements as req_rules
from db.connection import connect
from mcp_server import eval_queries
from mcp_server import pdr_approvals
from mcp_server import pdr_queries as q
from mcp_server import pdr_store as store
from mcp_server import server
from tests.pdr_helpers import (
    LICENSE_CONSTRAINT,
    NEED,
    PASS,
    PDR,
    REQS,
    add_dependency,
    add_interface,
    add_license_evidence,
    add_unlinked_evidence,
    approve,
    bespoke_task,
    bind_newer_revision,
    confirm,
    decided_build,
    lock,
    locked_build,
    make_capability,
    record_need,
    record_reqs,
    scalar,
    scoped,
    stage_succeeded,
    with_requirements,
)
from workers.recommend import _load_candidate_inputs

BOOKINGS = next(r for r in REQS if r["slug"] == "todays-bookings")
BOOKINGS_PASS = {**PASS, "req_id": "todays-bookings", "criterion_ordinal": 1}
BOOKINGS_FAIL = {"outcome": "fail", "req_id": "todays-bookings", "criterion_ordinal": 1}
BOOKINGS_NOT_RUN = {"outcome": "not_run", "req_id": "todays-bookings", "criterion_ordinal": 1}
EXTRA = {"slug": "glossary-note", "text": "Glossary is kept", "priority": "could",
         "quote": "PDR means product design requirements.", "disposition": "bespoke",
         "disposition_reason": "trivial", "acceptance_criteria": ["Glossary page exists"]}
POSTMARK = {"name": "Postmark", "category": "email", "purpose": "mail", "cost": "free",
            "data_leaves_infra": False}
SENTRY = {"name": "Sentry", "category": "other", "purpose": "errors", "cost": "free",
          "data_leaves_infra": True}
NO_GPL_THING = {"kind": "forbidden_dependency", "ecosystem": "pypi", "name": "gpl-thing"}
AUTHORIZE = {"kind": "required_interface", "name": "authorize"}


def audit_count(conn, event_type: str) -> int:
    return scalar(conn, "SELECT count(*) FROM audit_event WHERE event_type = %s", (event_type,))


def flags_for(result: dict, code: str) -> list[str]:
    return [f["detail"] for f in result["human_approval_items"] if f["code"] == code]


def in_progress(conn, ctx, **task) -> str:
    tid = bespoke_task(conn, ctx, **task)["build_task_id"]
    assert q.record_build_progress(conn, build_task_id=tid,
                                   status="in_progress")["status"] == "in_progress"
    return tid


def bookings(conn, pid: str) -> dict:
    cov = q.coverage(conn, project_id=pid)
    return next(r for r in cov["by_requirement"] if r["slug"] == "todays-bookings")


@pytest.fixture
def cli_db(monkeypatch, conn):
    monkeypatch.setattr(cli, "connect", lambda: connect(for_tests=True))
    return conn


# ---------------------------------------------------------------------------
# H1. Evidence CIP said it held, and the matcher never read
# ---------------------------------------------------------------------------

class TestTheMatcherReadsWhatAnalysisFound:
    """
    A component found by a metadata ingest already has a source binding, so
    promotion is a no-op for it and nothing analysis finds is ever linked
    to the version. Its stages had succeeded: CIP called the evidence
    sufficient, the matcher read the link tables, found nothing, and CIP
    adopted a component whose manifest named the forbidden dependency.
    """

    def _analysed_unlinked(self, conn) -> str:
        _, cv = make_capability(conn, license="MIT")
        stage_succeeded(conn, cv, "manifests")
        stage_succeeded(conn, cv, "interfaces")
        add_unlinked_evidence(conn, cv, "dependency",
                              {"ecosystem": "pypi", "name": "gpl-thing", "kind": "runtime"})
        add_unlinked_evidence(conn, cv, "interface",
                              {"kind": "function", "name": "authorize", "language": "python"})
        return cv

    def test_a_forbidden_dependency_in_unlinked_evidence_is_found(self, conn):
        cv = self._analysed_unlinked(conn)
        assert scalar(conn, "SELECT count(*) FROM capability_dependency") == 0
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT, NO_GPL_THING])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["capability_version_id"] == cv and cand["evidence"] == "sufficient"
        assert cand["confirmed_blocking_gaps"] == ["forbidden_dependency_present"]
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert d["verdict"] != "ADOPT" and "overrides_blocking_gap" in d["flags"]

    def test_a_required_interface_in_unlinked_evidence_is_found(self, conn):
        self._analysed_unlinked(conn)
        ctx = scoped(conn, constraints=[AUTHORIZE])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "sufficient" and cand["blocking_gap_count"] == 0
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert (d["verdict"], d["decided_by"]) == ("ADOPT", "cip_rules")

    def test_unlinked_rows_are_mapped_the_way_promotion_maps_them(self, conn):
        _, cv = make_capability(conn, license="MIT")
        add_unlinked_evidence(conn, cv, "interface", {"name": "authorize"})
        add_unlinked_evidence(conn, cv, "dependency", {"name": "requests"})
        inputs = _load_candidate_inputs(conn, cv)
        assert [(i.kind, i.name, i.signature, i.language) for i in inputs.interfaces] == [
            ("unknown", "authorize", "", None)]
        assert [(d.ecosystem, d.name, d.kind) for d in inputs.dependencies] == [
            ("unknown", "requests", "runtime")]

    def test_a_linked_row_is_read_once(self, conn):
        _, cv = make_capability(conn, license="MIT")
        add_interface(conn, cv, "authorize")
        add_dependency(conn, cv, "pypi", "requests")
        add_unlinked_evidence(conn, cv, "interface", {"name": "refresh"})
        inputs = _load_candidate_inputs(conn, cv)
        assert sorted(i.name for i in inputs.interfaces) == ["authorize", "refresh"]
        assert [(d.ecosystem, d.name) for d in inputs.dependencies] == [("pypi", "requests")]

    def test_unlinked_evidence_from_an_older_revision_is_not_read(self, conn):
        _, cv = make_capability(conn, license="MIT")
        add_unlinked_evidence(conn, cv, "interface", {"name": "authorize"})
        add_unlinked_evidence(conn, cv, "dependency", {"ecosystem": "pypi", "name": "gpl-thing"})
        bind_newer_revision(conn, cv, license="MIT")
        inputs = _load_candidate_inputs(conn, cv)
        assert inputs.interfaces == () and inputs.dependencies == ()
        missing = store.missing_evidence(
            conn, [cv], ["required_interface", "forbidden_dependency"])[cv]
        assert missing == ["interfaces", "dependencies"]

    def test_what_counts_as_held_is_what_the_matcher_is_given(self, conn):
        """Raw evidence with no stage result, and a stage that found nothing: both held."""
        _, raw = make_capability(conn, name="raw-lib", license="MIT")
        add_unlinked_evidence(conn, raw, "dependency", {"ecosystem": "pypi", "name": "x"})
        _, empty = make_capability(conn, name="empty-lib", license="MIT")
        stage_succeeded(conn, empty, "manifests")
        _, none = make_capability(conn, name="none-lib", license="MIT")
        missing = store.missing_evidence(conn, [raw, empty, none], ["forbidden_dependency"])
        assert missing[raw] == [] and missing[empty] == []
        assert missing[none] == ["dependencies"]
        assert len(_load_candidate_inputs(conn, raw).dependencies) == 1


# ---------------------------------------------------------------------------
# H2. A license nobody identified, read by the matcher
# ---------------------------------------------------------------------------

class TestALowConfidenceLicenseIsNotALicense:
    """
    "Only an identified license is evidence" held for what CIP reported and
    not for what the matcher read. GPL-2.0 identified, plus a file the
    extractor could only call BSD-2-Clause at low confidence: with
    BSD-2-Clause on the allowlist the candidate passed.
    """
    ALLOW = [{"kind": "license_allowlist", "spdx_ids": ["MIT", "BSD-2-Clause"]}]

    def test_the_matcher_reads_only_identified_licenses(self, conn):
        _, cv = make_capability(conn, name="gpl-lib", license="GPL-2.0")
        add_license_evidence(conn, cv, "BSD-2-Clause", confidence="low")
        add_license_evidence(conn, cv, "unknown")
        add_license_evidence(conn, cv, "NOASSERTION")
        assert [lic.spdx_id for lic in _load_candidate_inputs(conn, cv).licenses] == ["GPL-2.0"]

    def test_an_identified_gpl_is_a_gap_whatever_else_was_guessed(self, conn):
        _, cv = make_capability(conn, name="gpl-lib", display="OAuth GPL lib", license="GPL-2.0")
        add_license_evidence(conn, cv, "BSD-2-Clause", confidence="low")
        ctx = scoped(conn, constraints=self.ALLOW)
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["license"] == "GPL-2.0" and cand["evidence"] == "sufficient"
        assert cand["confirmed_blocking_gaps"] == ["license_mismatch"]
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert (d["verdict"], d["computed_verdict"]) == ("BUILD", "REJECT")

    def test_a_guess_alone_is_no_evidence_either_way(self, conn):
        _, cv = make_capability(conn, name="x-lib", display="OAuth X lib")
        add_license_evidence(conn, cv, "MIT", confidence="low")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        cand = q.search_for_requirement(conn, req_id=ctx.need_id)["candidates"][0]
        assert cand["evidence"] == "insufficient" and cand["missing_evidence"] == ["license"]
        assert cand["confirmed_blocking_gaps"] == [] and cand["license"] is None
        assert _load_candidate_inputs(conn, cv).licenses == ()


# ---------------------------------------------------------------------------
# H3. Two needs, one capability, two pins
# ---------------------------------------------------------------------------

class TestEachLockLineDescribesItsOwnPin:
    """
    Facts were looked up per capability version. Two needs that settled on
    the same version at different revisions shared one entry, and one of
    the two lock lines printed the other revision's license.
    """

    def _two_pins(self, conn):
        _, cv = make_capability(conn, license="MIT")                     # R1: MIT
        ctx = with_requirements(conn)
        first = record_need(conn, ctx.pid, constraints=[LICENSE_CONSTRAINT])
        second = record_need(
            conn, ctx.pid, slug="token-refresh", description="OAuth token refresh",
            search_terms=["oauth library"],
            constraints=[{"kind": "license_allowlist", "spdx_ids": ["MIT", "GPL-3.0"]}])
        confirm(conn, ctx.pid)
        q.search_for_requirement(conn, req_id=first["need_id"])
        d1 = q.record_decision(conn, req_id=first["need_id"])
        bind_newer_revision(conn, cv, "rev-NEW", license="GPL-3.0")      # R2: GPL-3.0
        q.search_for_requirement(conn, req_id=second["need_id"])
        d2 = q.record_decision(conn, req_id=second["need_id"])
        assert (d1["pinned_revision"], d2["pinned_revision"]) == ("rev-oauth-library", "rev-NEW")
        return ctx, cv

    def test_each_decision_carries_the_license_of_its_own_revision(self, conn):
        ctx, _ = self._two_pins(conn)
        for _ in range(3):                       # it used to depend on dict order
            first = q.lock_architecture(conn, project_id=ctx.pid)
            req = pdr_approvals.get_request(conn, first["approval_request_id"])
            by_slug = {d["need_slug"]: d for d in req["subject"]["decisions"]}
            assert (by_slug["oauth-login"]["pinned_revision"],
                    by_slug["oauth-login"]["license"]) == ("rev-oauth-library", "MIT")
            assert (by_slug["token-refresh"]["pinned_revision"],
                    by_slug["token-refresh"]["license"]) == ("rev-NEW", "GPL-3.0")

    def test_the_summary_and_the_flag_agree(self, conn):
        ctx, _ = self._two_pins(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        rows = first["summary_markdown"].splitlines()
        at = {slug: next(i for i, r in enumerate(rows) if r.startswith(f"- {slug} ["))
              for slug in ("oauth-login", "token-refresh")}
        assert "@rev-oauth-library" in rows[at["oauth-login"] + 1]
        assert rows[at["oauth-login"] + 2].strip().startswith("license: MIT (read from source)")
        assert "@rev-NEW" in rows[at["token-refresh"] + 1]
        assert rows[at["token-refresh"] + 2].strip().startswith("license: GPL-3.0")
        flagged = [f["subject"] for f in first["human_approval_items"]
                   if f["code"] == "non_permissive_license"]
        assert flagged == ["token-refresh"]

    def test_facts_are_keyed_by_decision(self, conn):
        ctx, cv = self._two_pins(conn)
        current = list(store.current_decisions(conn, ctx.pid).values())
        facts = store.decision_facts(conn, current)
        assert {d["id"] for d in current} == set(facts)
        assert sorted(f["license"] for f in facts.values()) == ["GPL-3.0", "MIT"]
        # A decision made now would pin the newest revision.
        assert store.capability_facts(conn, [cv])[cv]["license"] == "GPL-3.0"


# ---------------------------------------------------------------------------
# H4. A word written entirely in look-alikes
# ---------------------------------------------------------------------------

LOOKALIKES = {
    "mathematical sans-serif": "\U0001d5c8\U0001d5ba\U0001d5ce\U0001d5cd\U0001d5c1",
    "fullwidth": "ｏａｕｔｈ",
    "all-Cyrillic cache": "сасһе",
    "all-Cyrillic sso": "ѕѕо",
    "script g": "ɡraphql",
    "latin alpha": "oɑuth",
    "dotless i": "oıdc",
}


class TestASearchTermIsPlainAscii:
    """
    A word mixing Latin with a Cyrillic look-alike was refused. A word
    written entirely in look-alikes was not: the human confirmed "cache",
    the registry was asked five Cyrillic letters, and "nothing in the
    registry" was recorded as CIP's own finding.
    """

    @pytest.mark.parametrize("label", sorted(LOOKALIKES))
    def test_a_lookalike_term_is_refused(self, conn, label):
        ctx = with_requirements(conn)
        out = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "search_terms": [LOOKALIKES[label]]}])["needs"][0]
        assert not out["valid"]
        assert "plain ASCII" in out["errors"][0] and "U+" in out["errors"][0]
        assert out["errors"][0].isascii()            # shown escaped: nothing to mistake

    def test_the_lookalike_no_longer_hides_a_match(self, conn):
        make_capability(conn, license="MIT")
        ctx = with_requirements(conn)
        refused = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "search_terms": [LOOKALIKES["latin alpha"]],
             "constraints": [LICENSE_CONSTRAINT]}])["needs"][0]
        assert not refused["valid"]
        need = record_need(conn, ctx.pid, search_terms=["oauth"],
                           constraints=[LICENSE_CONSTRAINT])
        confirm(conn, ctx.pid)
        assert q.search_for_requirement(conn, req_id=need["need_id"])["candidate_count"] == 1

    def test_one_spelling_of_a_term(self, conn):
        """Two spaces displayed as one and matched nothing; a capital I read as a small l."""
        make_capability(conn, name="oauth-login-kit", display="OAuth Login Kit", license="MIT")
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid, search_terms=["OAuth   Login", "oauth login", "F#"],
                           constraints=[LICENSE_CONSTRAINT])
        stored = scalar(conn, "SELECT search_terms FROM project_requirement WHERE id = %s",
                        (need["need_id"],))
        assert sorted(stored) == ["f#", "oauth login"]
        summary = q.request_requirements_confirmation(conn, project_id=ctx.pid)["summary_markdown"]
        assert '(2 term(s)): "f#", "oauth login"' in summary or \
               '(2 term(s)): "oauth login", "f#"' in summary
        approve(conn, q.request_requirements_confirmation(
            conn, project_id=ctx.pid)["approval_request_id"])
        assert q.search_for_requirement(conn, req_id=need["need_id"])["candidate_count"] == 1


class TestAProviderNameCipCannotCheck:
    """
    The known-provider list is ASCII names. "ОКТА" in Cyrillic reads as
    Okta and matched nothing on it, so a known auth provider declared as
    "other" reached the human with no flag.
    """
    BASE = {"category": "other", "purpose": "staff single sign-on", "cost": "free",
            "data_leaves_infra": False}

    @pytest.mark.parametrize("name", ["ОКТА", "ΟΚΤΑ",
                                      "РАУРАӀ", "Strıpe",
                                      "Яндекс"])
    def test_a_name_outside_ascii_is_put_in_front_of_the_human(self, conn, name):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid,
                                    approved_providers=[{**self.BASE, "name": name}])
        assert first["status"] == "awaiting_approval"
        details = flags_for(first, "sensitive_provider")
        assert len(details) == 1 and "not written in plain ASCII" in details[0]
        assert "sensitive_provider" in first["summary_markdown"]

    def test_an_ascii_name_is_judged_as_before(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[
            {**self.BASE, "name": "OKTA"}, {**self.BASE, "name": "Postmark"},
            {**self.BASE, "name": "Stripé"}])
        details = flags_for(first, "sensitive_provider")
        assert len(details) == 2
        assert any("known auth provider" in d for d in details)
        assert any("known payment provider" in d for d in details)      # folded, then matched

    def test_a_declared_sensitive_category_needs_no_second_flag(self):
        payload = lock_rules.build_lock_payload(
            project_id="p", requirements_hash="h", architecture_doc_hash=None, decisions=[],
            project_decisions=[], cost_commitments=[], unresolved_risks=[], scope={},
            approved_providers=[{**self.BASE, "name": "ОКТА",
                                 "category": "auth"}])
        details = [f["detail"] for f in lock_rules.lock_flags(payload)
                   if f["code"] == "sensitive_provider"]
        assert details == ["external auth provider"]


# ---------------------------------------------------------------------------
# M1. A lock approval given before the scope moved twice
# ---------------------------------------------------------------------------

class TestALockApprovalIsForOneConfirmation:
    """
    Scope A confirmed; a lock approved under it and not written; scope B
    confirmed; back to A, confirmed again. The requirements hash is A's
    throughout, and the old lock approval was written.
    """

    def _round_trip(self, conn, ctx):
        added = record_reqs(conn, ctx.pid, ctx.sid, [EXTRA])["requirements"][0]
        confirm(conn, ctx.pid)                                           # B
        q.retire_requirement(conn, req_id=added["req_id"], reason="changed my mind")
        confirm(conn, ctx.pid)                                           # A, a second time

    def test_the_old_approval_is_not_written(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        approve(conn, first["approval_request_id"])
        assert q.pdr_status(conn, project_id=ctx.pid)["approved_lock_request"] is not None
        self._round_trip(conn, ctx)

        assert q.pdr_status(conn, project_id=ctx.pid)["approved_lock_request"] is None
        replay = q.lock_architecture(conn, project_id=ctx.pid,
                                     approval_request_id=first["approval_request_id"])
        assert replay["refused"] and "confirmed again" in replay["reason"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0

        again = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        assert again["status"] == "awaiting_approval"
        assert again["approval_request_id"] != first["approval_request_id"]

    def test_a_pending_request_from_before_stays_dead(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        self._round_trip(conn, ctx)
        described = q.describe_request(conn, first["approval_request_id"])
        assert "confirmed again" in described["obsolete_reason"]

    def test_the_payload_names_the_confirmation(self, conn):
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        confirmation = scalar(
            conn, "SELECT a.id::text FROM approval a JOIN approval_request ar "
                  "ON ar.id = a.approval_request_id WHERE ar.kind = 'requirements_confirmation'")
        subject = pdr_approvals.get_request(conn, first["approval_request_id"])["subject"]
        assert subject["requirements_approval_id"] == confirmation
        tail = first["summary_markdown"].splitlines()[-2:]
        assert tail == [f"Made under requirements confirmation: {confirmation[:8]}",
                        "Follows lock request: none (the project's first)"]
        approve(conn, first["approval_request_id"])
        written = q.lock_architecture(conn, project_id=ctx.pid,
                                      approval_request_id=first["approval_request_id"])
        assert scalar(conn, "SELECT requirements_approval_id::text FROM architecture_lock "
                            "WHERE id = %s", (written["architecture_lock_id"],)) == confirmation
        # The stored lock rebuilds to the hash that was approved.
        active = store.load_active_lock(conn, ctx.pid)
        assert lock_rules.payload_hash(store.lock_payload(active)) == active["payload_hash"]

    def test_a_written_lock_is_not_reopened_by_the_round_trip(self, conn):
        """History, not content: the same lock for the same scope needs no second approval."""
        ctx = decided_build(conn)
        lock(conn, ctx.pid, approved_providers=[POSTMARK])
        self._round_trip(conn, ctx)
        st = q.pdr_status(conn, project_id=ctx.pid)
        assert st["lock"]["usable"] is True
        same = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        assert same.get("unchanged") is True

    def test_the_confirmation_is_history_not_content(self):
        def payload(confirmation):
            return lock_rules.build_lock_payload(
                project_id="p", requirements_hash="h", architecture_doc_hash=None,
                decisions=[], project_decisions=[], approved_providers=[], cost_commitments=[],
                unresolved_risks=[], scope={}, requirements_approval_id=confirmation)
        a, b = payload("a1"), payload("a2")
        assert lock_rules.payload_hash(a) != lock_rules.payload_hash(b)
        assert lock_rules.content_core_hash(a) == lock_rules.content_core_hash(b)
        assert lock_rules.diff_payloads(a, b)["changes"] == []


# ---------------------------------------------------------------------------
# M2. "Latest" by when the transaction started
# ---------------------------------------------------------------------------

class TestTheLatestRequestIsTheLastOneAsked:
    """
    The latest lock request was read by audit_event.created_at, which is
    the start of the writing transaction. A call that began first and
    finished last was the newest request with the oldest timestamp: it was
    reported as superseded, and asking for it again returned the same
    request, which nobody could approve.
    """

    def test_a_call_that_started_first_and_finished_last_is_the_latest(self, conn):
        ctx = decided_build(conn)
        early, late = connect(for_tests=True), connect(for_tests=True)
        try:
            assert store.get_project(early, ctx.pid)        # X's transaction starts here
            y = q.lock_architecture(late, project_id=ctx.pid, approved_providers=[SENTRY])
            late.rollback()
            x = q.lock_architecture(early, project_id=ctx.pid, approved_providers=[POSTMARK])
            early.rollback()
        finally:
            early.close()
            late.close()
        conn.rollback()
        assert pdr_approvals.latest_requested_lock_request(conn, ctx.pid) == \
            x["approval_request_id"]
        assert q.describe_request(conn, x["approval_request_id"])["obsolete_reason"] is None
        assert q.describe_request(conn, y["approval_request_id"])["obsolete_reason"]
        approve(conn, x["approval_request_id"])
        written = q.lock_architecture(conn, project_id=ctx.pid,
                                      approval_request_id=x["approval_request_id"])
        assert written["version"] == 1

    def test_audit_events_are_numbered_in_the_order_written(self, conn):
        ctx = decided_build(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT seq, created_at FROM audit_event WHERE project_id = %s "
                        "ORDER BY seq", (ctx.pid,))
            rows = cur.fetchall()
        assert len({r[0] for r in rows}) == len(rows) > 3
        assert [r[1] for r in rows] == sorted(r[1] for r in rows)

    def test_one_lock_call_per_project_at_a_time(self, conn):
        ctx = decided_build(conn)
        other = connect(for_tests=True)
        try:
            q._one_at_a_time(conn, ctx.pid)                 # a call in flight
            with other.cursor() as cur:
                cur.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                q.lock_architecture(other, project_id=ctx.pid)
            other.rollback()
            conn.rollback()                                 # the first call ends
            assert q.lock_architecture(
                other, project_id=ctx.pid)["status"] == "awaiting_approval"
        finally:
            other.close()

    def test_a_returned_request_holds_nothing(self, conn):
        """The per-project lock ends with the call, whatever the call returned."""
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid)
        assert first["status"] == "awaiting_approval"
        bad = q.lock_architecture(conn, project_id=ctx.pid, approval_request_id="nope")
        assert "error" in bad
        other = connect(for_tests=True)
        try:
            with other.cursor() as cur:
                cur.execute("SET lock_timeout = '300ms'")
            again = q.lock_architecture(other, project_id=ctx.pid)
            assert again["approval_request_id"] == first["approval_request_id"]
        finally:
            other.close()


# ---------------------------------------------------------------------------
# M3. The half of the sweep that was not in the fingerprint
# ---------------------------------------------------------------------------

class TestTheSweepCoversTheEvaluationStore:

    def test_an_evaluation_banked_after_the_sweep_asks_for_a_new_one(self, conn):
        """It did not: the BUILD was recorded as a cold start with nothing to compare."""
        ctx = scoped(conn)
        q.search_for_requirement(conn, req_id=ctx.need_id)
        before = store.registry_stamp(conn)
        eval_queries.record_evaluation(
            conn, asset_slug="acme-oauth-kit", verdict="Adopt", asset_type="repo",
            url="https://github.com/acme/oauth-kit", banked=True, evidence_depth="code_read",
            code_quality="solid", summary="Drop-in OAuth2 login", problem_types=["oauth-flow"],
            license_notes="MIT")
        assert store.registry_stamp(conn) != before
        refused = q.record_decision(conn, req_id=ctx.need_id)
        assert refused["refused"] and "an evaluation was recorded" in refused["reason"]
        q.search_for_requirement(conn, req_id=ctx.need_id)
        d = q.record_decision(conn, req_id=ctx.need_id)
        assert "build_despite_banked_match" in d["flags"]

    def test_a_version_changing_state_moves_the_fingerprint(self, conn):
        _, cv = make_capability(conn, license="MIT")
        before = store.registry_stamp(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE capability_version SET lifecycle_state = 'deprecated' "
                        "WHERE id = %s", (cv,))
        conn.commit()
        assert store.registry_stamp(conn) != before

    def test_nothing_changed_nothing_moves(self, conn):
        make_capability(conn, license="MIT")
        assert store.registry_stamp(conn) == store.registry_stamp(conn)


# ---------------------------------------------------------------------------
# M4. A scaffold that fails halfway
# ---------------------------------------------------------------------------

class TestAFileFromAScaffoldThatRaised:

    def test_the_file_it_left_behind_is_still_labelled(self, conn, tmp_path, monkeypatch):
        """
        README.md is written before the edges are read. `edges: [{}]` raised
        after it was on disk, the log ran only after a clean return, and the
        file was ingested as a document a person had supplied.
        """
        monkeypatch.setattr(server, "connect", lambda: connect(for_tests=True))
        body = ("ignored\n\n# Clinic PDR\n\n## Requirements\n1. Staff must sign in with "
                "Google OAuth before using the system.\n")
        with pytest.raises(KeyError):
            server.scaffold_pipeline({"name": "Clinic PDR", "intent": body, "stages": [],
                                      "edges": [{}]}, str(tmp_path / "docs"))
        readme = tmp_path / "docs" / "README.md"
        assert readme.exists()
        assert audit_count(conn, "file_written_by_tool") >= 1

        ing = q.ingest_pdr(conn, project_name="clinic", pdr_title="Clinic PDR",
                           pdr_path=str(readme))
        record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "sign-in", "text": "Staff sign in",
            "quote": "Staff must sign in with Google OAuth before using the system.",
            "acceptance_criteria": ["signs in"], "disposition": "bespoke",
            "disposition_reason": "x"}])
        summary = q.request_requirements_confirmation(
            conn, project_id=ing["project_id"])["summary_markdown"]
        assert "THIS FILE WAS WRITTEN BY A CIP TOOL (scaffold_pipeline)" in summary

    def test_what_a_failed_scaffold_could_have_written(self, tmp_path):
        (tmp_path / "stages" / "one").mkdir(parents=True)
        for rel in ("README.md", "pipeline.yaml", "notes.md", "stages/one/README.md",
                    "stages/one/TODO.md", "stages/one/other.md"):
            (tmp_path / rel).write_text("x", encoding="utf-8")
        assert server._scaffold_files_on_disk(tmp_path) == [
            "pipeline.yaml", "README.md", "stages/one/README.md", "stages/one/TODO.md"]
        assert server._scaffold_files_on_disk(tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# M5. A fail erased by an edit and its undo
# ---------------------------------------------------------------------------

class TestAFailSurvivesARewrite:
    """
    Rewording a requirement deleted its criteria, and every verification
    with them. Change the text and change it back: the confirmed hash
    returns, no human is involved, and a failure CIP itself observed is gone.
    """

    def _cip_fail(self, conn, ctx) -> str:
        tid = in_progress(conn, ctx)
        out = q.record_executed_verification(conn, build_task_id=tid, verification={
            **BOOKINGS_FAIL, "command": "pytest", "exit_code": 1, "commit_sha": "abc1234",
            "test_ref": "t"})
        assert out["outcome"] == "fail", out
        return tid

    def test_edit_and_revert_keeps_the_fail(self, conn):
        ctx = locked_build(conn)
        self._cip_fail(conn, ctx)
        record_reqs(conn, ctx.pid, ctx.sid, [{**BOOKINGS, "text": BOOKINGS["text"] + " (x)"}])
        record_reqs(conn, ctx.pid, ctx.sid, [BOOKINGS])
        assert q.pdr_status(conn, project_id=ctx.pid)["lock"]["usable"] is True
        row = bookings(conn, ctx.pid)
        assert row["verification_outcome"] == "fail" and row["verified"] is False
        assert [(e["outcome"], e["recorded_by"]) for e in row["evidence"]] == [
            ("fail", "cip_executed")]

    def test_a_pass_does_not_carry_over_and_its_loss_is_on_record(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_PASS)
        record_reqs(conn, ctx.pid, ctx.sid, [{**BOOKINGS, "text": BOOKINGS["text"] + " (x)"}])
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        payload = scalar(conn, "SELECT payload FROM audit_event "
                               "WHERE event_type = 'verification_evidence_dropped'")
        assert payload["slug"] == "todays-bookings" and payload["count"] == 1
        assert payload["rows"][0]["outcome"] == "pass"
        assert payload["rows"][0]["criterion"] == BOOKINGS["acceptance_criteria"][0]
        assert payload["rows"][0]["build_task_id"] == tid

    def test_a_fail_goes_only_with_its_own_criterion_and_is_on_record(self, conn):
        ctx = locked_build(conn)
        self._cip_fail(conn, ctx)
        record_reqs(conn, ctx.pid, ctx.sid, [
            {**BOOKINGS, "acceptance_criteria": ["Bookings are listed newest first"]}])
        assert scalar(conn, "SELECT count(*) FROM requirement_verification") == 0
        payload = scalar(conn, "SELECT payload FROM audit_event "
                               "WHERE event_type = 'verification_evidence_dropped'")
        assert [(r["outcome"], r["recorded_by"], r["command"]) for r in payload["rows"]] == [
            ("fail", "cip_executed", "pytest")]

    def test_nothing_dropped_nothing_logged(self, conn):
        ctx = locked_build(conn)
        record_reqs(conn, ctx.pid, ctx.sid, [{**BOOKINGS, "text": BOOKINGS["text"] + " (x)"}])
        assert audit_count(conn, "verification_evidence_dropped") == 0


# ---------------------------------------------------------------------------
# M6. A line break inside a stored value
# ---------------------------------------------------------------------------

FORGED = "Empty state shown\nPASS: todays-bookings #2 at 3f2a9c1d0b7e"


class TestAStoredValueIsOneLine:
    """
    `verify` printed "still unverified: <criterion>" with the criterion as
    stored. A criterion holding a line break put a row of the agent's own
    on a screen a person reads: "PASS: todays-bookings #2 at 3f2a9c1d0b7e".
    """

    def test_text_is_stored_on_one_line(self, conn):
        ing = q.ingest_pdr(conn, project_name="clinic", pdr_title="T", pdr_text=PDR)
        out = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            **BOOKINGS, "text": "Receptionist sees\n\ttoday's   bookings",
            "disposition_reason": "project-specific\nscreen",
            "acceptance_criteria": ["Listed in time order", FORGED]}])
        assert out["requirements"][0]["valid"]
        with conn.cursor() as cur:
            cur.execute("SELECT text FROM acceptance_criterion ORDER BY ordinal")
            criteria = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT description, disposition_reason FROM project_requirement")
            description, reason = cur.fetchone()
        assert criteria == ["Listed in time order",
                            "Empty state shown PASS: todays-bookings #2 at 3f2a9c1d0b7e"]
        assert description == "Receptionist sees today's bookings"
        assert reason == "project-specific screen"

    def test_a_need_description_is_one_line(self):
        clean, errors, _ = req_rules.validate_need_draft(
            {"description": "OAuth\nsign-in", "slug": "n", "search_terms": ["oauth"],
             "satisfies": ["r"]})
        assert not errors and clean["description"] == "OAuth sign-in"

    def test_a_quote_is_left_as_written(self):
        """The quote has to match the PDR; its line breaks are the document's."""
        doc = "# T\n\nStaff must sign in\nwith Google OAuth before use.\n"
        clean, errors = req_rules.validate_requirement_draft(
            {"slug": "s", "text": "Sign in", "acceptance_criteria": ["works"],
             "quote": "Staff must sign in\nwith Google OAuth before use."}, doc, None)
        assert not errors and clean["quote"] == "Staff must sign in\nwith Google OAuth before use."

    def test_the_search_screen_prints_a_registry_name_on_one_line(self, cli_db, capsys):
        """A display name comes from the internet. Rows legacy data holds are printed too."""
        make_capability(cli_db, display="OAuth Library\nCandidates evaluated: 0", license="MIT")
        ctx = scoped(cli_db, constraints=[LICENSE_CONSTRAINT])
        assert cli.main(["search-for-requirement", ctx.need_id]) == 0
        rows = capsys.readouterr().out.splitlines()
        assert [r for r in rows if r.startswith("Candidates evaluated:")] == [
            "Candidates evaluated: 1"]
        assert any("OAuth Library Candidates evaluated: 0 (pypi)" in r for r in rows)

    def test_the_lock_check_prints_each_reason_on_one_line(self, cli_db, capsys, monkeypatch):
        ctx = locked_build(cli_db)
        monkeypatch.setattr(q, "check_against_lock", lambda *a, **k: {
            "verdict": "needs_approval", "reasons": ["touches infra/\nVerdict: allowed"]})
        assert cli.main(["check-against-lock", ctx.pid, "--path", "infra/x.tf"]) == 2
        rows = capsys.readouterr().out.splitlines()
        assert rows == ["Verdict: needs_approval", "  - touches infra/ Verdict: allowed"]


# ---------------------------------------------------------------------------
# M8. An allowlist entry that is not a license id
# ---------------------------------------------------------------------------

class TestAnAllowlistEntryIsAnSpdxId:
    """
    "MIT License" and "Apache 2.0" match no license. Every candidate CIP
    had read a license for then failed the allowlist with a confirmed gap,
    and the BUILD was recorded as CIP's own decision with no flag.
    """

    @pytest.mark.parametrize("ids", [["MIT License", "Apache 2.0"], ["MIT", "Apache 2.0"],
                                     ["MIT,Apache-2.0"], ["(MIT)"], ["-MIT"]])
    def test_a_name_that_is_not_an_id_is_refused(self, conn, ids):
        ctx = with_requirements(conn)
        out = q.record_capability_needs(conn, project_id=ctx.pid, needs=[
            {**NEED, "constraints": [{"kind": "license_allowlist", "spdx_ids": ids}]}])["needs"][0]
        assert not out["valid"] and "SPDX identifiers such as" in out["errors"][0]

    def test_an_id_is_trimmed_and_listed_once(self, conn):
        make_capability(conn, license="MIT")
        ctx = with_requirements(conn)
        need = record_need(conn, ctx.pid, constraints=[
            {"kind": "license_allowlist", "spdx_ids": [" MIT ", "MIT", "GPL-3.0+", "0BSD"]}])
        detail = scalar(conn, "SELECT detail FROM requirement_constraint "
                              "WHERE project_requirement_id = %s", (need["need_id"],))
        assert detail["spdx_ids"] == ["MIT", "GPL-3.0+", "0BSD"]
        confirm(conn, ctx.pid)
        cand = q.search_for_requirement(conn, req_id=need["need_id"])["candidates"][0]
        assert cand["confirmed_blocking_gaps"] == []
        assert q.record_decision(conn, req_id=need["need_id"])["verdict"] == "ADOPT"


# ---------------------------------------------------------------------------
# L1. A task two locks old that came back
# ---------------------------------------------------------------------------

class TestALockEndsTheTasksItDoesNotCarry:

    def test_a_task_does_not_revive_when_its_requirement_returns(self, conn):
        """
        Verified under v1; the requirement deferred and relocked; brought
        back and relocked. The task was current again, with the status and
        the passes it had two locks ago.
        """
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        q.record_build_progress(conn, build_task_id=tid, status="implemented")
        assert q.record_build_progress(conn, build_task_id=tid, status="verified",
                                       verification=BOOKINGS_PASS)["status"] == "verified"

        record_reqs(conn, ctx.pid, ctx.sid, [{**BOOKINGS, "disposition": "deferred",
                                              "disposition_reason": "next release"}])
        confirm(conn, ctx.pid)
        assert lock(conn, ctx.pid)["tasks_abandoned"] == [tid]

        record_reqs(conn, ctx.pid, ctx.sid, [BOOKINGS])
        confirm(conn, ctx.pid)
        assert lock(conn, ctx.pid)["tasks_abandoned"] == []

        assert scalar(conn, "SELECT status FROM build_task WHERE id = %s", (tid,)) == "abandoned"
        row = bookings(conn, ctx.pid)
        assert (row["verified"], row["implemented"], row["verification_outcome"]) == (
            False, False, None)
        out = q.record_build_progress(conn, build_task_id=tid, status="verified")
        assert out["refused"] and "abandoned" in out["reason"]

    def test_a_task_the_lock_still_carries_is_left_alone(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        relock = lock(conn, ctx.pid, approved_providers=[POSTMARK])
        assert relock["tasks_abandoned"] == []
        assert scalar(conn, "SELECT status FROM build_task WHERE id = %s", (tid,)) == "in_progress"

    def test_the_terminal_says_which_tasks_a_lock_ended(self, cli_db, capsys):
        ctx = locked_build(cli_db)
        tid = in_progress(cli_db, ctx)
        record_reqs(cli_db, ctx.pid, ctx.sid, [{**BOOKINGS, "disposition": "deferred",
                                                "disposition_reason": "next release"}])
        confirm(cli_db, ctx.pid)
        first = q.lock_architecture(cli_db, project_id=ctx.pid)
        approve(cli_db, first["approval_request_id"])
        assert cli.main(["pdr-lock", ctx.pid, "--approval-request",
                         first["approval_request_id"]]) == 0
        rows = capsys.readouterr().out.splitlines()
        at = rows.index("  Tasks abandoned (not carried by this lock): 1")
        assert rows[at + 1] == f"    {tid}"

    def test_the_abandonment_is_on_record(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        record_reqs(conn, ctx.pid, ctx.sid, [{**BOOKINGS, "disposition": "deferred",
                                              "disposition_reason": "next release"}])
        confirm(conn, ctx.pid)
        lock(conn, ctx.pid)
        payload = scalar(conn, "SELECT payload FROM audit_event WHERE subject_id = %s "
                               "AND event_type = 'task_updated' ORDER BY seq DESC LIMIT 1", (tid,))
        assert payload == {"from": "in_progress", "to": "abandoned",
                           "reason": "not carried by lock v2"}


# ---------------------------------------------------------------------------
# L2, L3. Evidence that counted when it should not, and a fail that did not
# ---------------------------------------------------------------------------

class TestWhatTheLatestWordIs:

    def test_a_pass_does_not_count_while_its_task_is_back_at_planned(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_PASS)
        assert bookings(conn, ctx.pid)["verification_outcome"] == "pass"
        assert q.record_build_progress(conn, build_task_id=tid,
                                       status="planned")["status"] == "planned"
        assert bookings(conn, ctx.pid)["verification_outcome"] is None
        q.record_build_progress(conn, build_task_id=tid, status="in_progress")
        assert bookings(conn, ctx.pid)["verification_outcome"] == "pass"

    def test_not_run_does_not_hide_a_fail(self, conn):
        """'I did not run it', filed after a failure, read as merely unverified."""
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_FAIL)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_NOT_RUN)
        row = bookings(conn, ctx.pid)
        assert row["verification_outcome"] == "fail"
        assert row["evidence"][0]["outcome"] == "fail"

    def test_not_run_still_replaces_a_pass(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_PASS)
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_NOT_RUN)
        row = bookings(conn, ctx.pid)
        assert row["evidence"][0]["outcome"] == "not_run"
        assert row["verification_outcome"] is None and row["verified"] is False

    def test_a_pass_after_the_fail_is_the_latest_word(self, conn):
        ctx = locked_build(conn)
        tid = in_progress(conn, ctx)
        for v in (BOOKINGS_FAIL, BOOKINGS_NOT_RUN, BOOKINGS_PASS):
            q.record_build_progress(conn, build_task_id=tid, verification=v)
        assert bookings(conn, ctx.pid)["verification_outcome"] == "pass"
        q.record_build_progress(conn, build_task_id=tid, verification=BOOKINGS_NOT_RUN)
        assert bookings(conn, ctx.pid)["evidence"][0]["outcome"] == "not_run"


# ---------------------------------------------------------------------------
# L4, L6, L7, L8. Smaller things
# ---------------------------------------------------------------------------

class TestSmallerThingsFourthRound:

    @pytest.mark.parametrize("rationale", ["​", "​ ﻿", "́"])
    def test_a_rationale_nobody_can_read_is_no_rationale(self, conn, rationale):
        make_capability(conn, license="MIT")
        ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
        q.search_for_requirement(conn, req_id=ctx.need_id)
        out = q.record_decision(conn, req_id=ctx.need_id, verdict="BUILD", rationale=rationale)
        assert out["refused"] and "rationale is required" in out["reason"]

    def test_a_passage_longer_than_the_quote_that_found_it(self, conn):
        """Matched with whitespace collapsed: 80 characters of quote, 700 of PDR."""
        words = ["Staff", "must", "sign", "in", "with", "a", "Google", "account", "today."]
        doc = "# T\n\n## Requirements\n" + (" " * 90).join(words) + "\n\n## Other\nBody text.\n"
        ing = q.ingest_pdr(conn, project_name="clinic", pdr_title="T", pdr_text=doc)
        out = record_reqs(conn, ing["project_id"], ing["requirement_source_id"], [{
            "slug": "sign-in", "text": "Staff sign in", "quote": " ".join(words),
            "acceptance_criteria": ["signs in"], "disposition": "bespoke",
            "disposition_reason": "x"}])["requirements"][0]
        assert not out["valid"] and "as it stands in the PDR" in out["errors"][0]

    def test_an_anchor_that_blocks_confirmation_maps_no_section(self, conn):
        ctx = with_requirements(conn)
        scope = store.load_scope(conn, ctx.pid)
        source = store.latest_source(conn, ctx.pid)
        before = store.scope_span_coverage(scope, source)
        # An anchor stored under older rules: it runs from Requirements into Glossary.
        start, end = PDR.index("1. Staff"), PDR.index("PDR means") + 10
        with conn.cursor() as cur:
            cur.execute("UPDATE project_requirement SET source_span = %s::jsonb WHERE id = %s",
                        (json.dumps({"start": start, "end": end, "quote": PDR[start:end]}),
                         ctx.reqs["dark-mode"]))
        conn.commit()
        scope = store.load_scope(conn, ctx.pid)
        row = next(r for r in scope["requirements"] if r["slug"] == "dark-mode")
        assert row["span_issue"] == "crosses_heading"
        after = store.scope_span_coverage(scope, source)
        assert after["unmapped_sections"] == before["unmapped_sections"]
        assert any("dark-mode" in b for b in store.scope_blockers(scope, source))

    def test_the_terminal_looks_again_before_it_records(self, cli_db, monkeypatch, capsys):
        """
        The summary was on screen while a decision changed under it. The
        code was typed, and the approval was recorded for a lock that would
        never be written.
        """
        ctx = decided_build(cli_db)
        first = q.lock_architecture(cli_db, project_id=ctx.pid)
        code = scalar(cli_db, "SELECT subject_hash FROM approval_request WHERE id = %s",
                      (first["approval_request_id"],))[:6]
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

        def reads_slowly(prompt=""):
            other = connect(for_tests=True)
            try:
                q.record_decision(other, req_id=ctx.need_id)       # a new decision record
            finally:
                other.close()
            return code
        monkeypatch.setattr("builtins.input", reads_slowly)
        assert cli.main(["approvals", "approve", first["approval_request_id"]]) == 1
        out = capsys.readouterr().out
        assert "The project changed while this was on screen" in out
        assert "Nothing was recorded" in out
        assert scalar(cli_db, "SELECT count(*) FROM approval a JOIN approval_request ar "
                              "ON ar.id = a.approval_request_id "
                              "WHERE ar.kind = 'architecture_lock'") == 0

    def test_an_unchanged_request_is_still_recorded(self, cli_db, monkeypatch, capsys):
        ctx = decided_build(cli_db)
        first = q.lock_architecture(cli_db, project_id=ctx.pid)
        code = scalar(cli_db, "SELECT subject_hash FROM approval_request WHERE id = %s",
                      (first["approval_request_id"],))[:6]
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda prompt="": code)
        assert cli.main(["approvals", "approve", first["approval_request_id"]]) == 0
        assert "Recorded: approved" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Tests added while injecting bugs: a mutant that survived was a rule
# nothing checked.
# ---------------------------------------------------------------------------

class TestWhatBugInjectionFound:

    def test_a_pin_is_a_revision_of_one_asset(self, conn):
        """Two repositories can hold a revision with the same key (a fork at one commit)."""
        ctx = decided_adopt_mit(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM source_provider LIMIT 1")
            provider = cur.fetchone()[0]
            cur.execute("INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
                        "VALUES (%s, 'fork/oauth-library', 'fork/oauth-library', 'repository') "
                        "RETURNING id", (provider,))
            cur.execute("INSERT INTO source_revision (source_asset_id, revision_key, "
                        "identified_at) VALUES (%s, 'rev-oauth-library', now() + interval "
                        "'1 hour') RETURNING id", (cur.fetchone()[0],))
            fork_rev = cur.fetchone()[0]
            cur.execute("INSERT INTO capability_source_binding (capability_version_id, "
                        "source_revision_id) VALUES (%s, %s)", (ctx.cv_id, fork_rev))
            cur.execute("INSERT INTO analysis_run (source_revision_id, "
                        "extractor_config_version, status) VALUES (%s, 1, 'completed') "
                        "RETURNING id", (fork_rev,))
            cur.execute("INSERT INTO evidence_item (source_revision_id, analysis_run_id, "
                        "extractor_name, evidence_type, locator_kind, locator, extracted_value) "
                        "VALUES (%s, %s, 'licenses', 'license', 'whole_file', "
                        "'{\"path\": \"LICENSE\"}'::jsonb, '{\"spdx_id\": \"GPL-3.0\"}'::jsonb)",
                        (fork_rev, cur.fetchone()[0]))
        conn.commit()
        decision = store.current_decisions(conn, ctx.pid)[ctx.need_id]
        assert decision["pinned_source_asset_key"] == "test/oauth-library"
        assert store.decision_facts(conn, [decision])[decision["id"]]["license"] == "MIT"

    def test_a_request_the_tool_returns_can_be_approved(self, conn):
        """
        If an older request is ever recorded as the latest (two calls that
        overlapped), asking for the newer one again makes it the latest.
        """
        ctx = decided_build(conn)
        a = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        b = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[SENTRY])
        store.audit(conn, project_id=ctx.pid, event_type="lock_approval_requested",
                    subject_table="approval_request", subject_id=a["approval_request_id"],
                    payload={})
        conn.commit()
        assert q.describe_request(conn, b["approval_request_id"])["obsolete_reason"]
        again = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[SENTRY])
        assert again["approval_request_id"] == b["approval_request_id"]
        assert q.describe_request(conn, b["approval_request_id"])["obsolete_reason"] is None
        assert scalar(conn, "SELECT payload->>'asked_again' FROM audit_event "
                            "WHERE event_type = 'lock_approval_requested' "
                            "ORDER BY seq DESC LIMIT 1") == "true"

    def test_a_request_made_while_a_lock_is_being_written_wins(self, conn, monkeypatch):
        """Between the request's commit and the write, the project is not held."""
        ctx = decided_build(conn)
        first = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        approve(conn, first["approval_request_id"])
        real, fired = pdr_approvals.request_state, []

        def in_the_gap(c, **kw):
            if not fired:                       # once: the other call reads the state too
                fired.append(True)
                other = connect(for_tests=True)
                try:
                    q.lock_architecture(other, project_id=ctx.pid, approved_providers=[SENTRY])
                finally:
                    other.close()
            return real(c, **kw)
        monkeypatch.setattr(pdr_approvals, "request_state", in_the_gap)
        out = q.lock_architecture(conn, project_id=ctx.pid,
                                  approval_request_id=first["approval_request_id"])
        assert out["refused"] and "A later lock request was made" in out["reason"]
        assert scalar(conn, "SELECT count(*) FROM architecture_lock") == 0

    def test_an_evaluation_rewritten_in_place_moves_the_fingerprint(self, conn):
        record = dict(asset_slug="acme-oauth-kit", asset_type="repo", banked=True,
                      url="https://github.com/acme/oauth-kit", evidence_depth="code_read",
                      code_quality="solid", summary="Drop-in OAuth2 login",
                      problem_types=["oauth-flow"], license_notes="MIT")
        eval_queries.record_evaluation(conn, verdict="Adopt", **record)
        before = store.registry_stamp(conn)
        eval_queries.record_evaluation(conn, verdict="Reject", **{**record, "banked": False})
        assert scalar(conn, "SELECT count(*) FROM evaluation") == 1
        assert store.registry_stamp(conn) != before

    def test_a_later_request_says_which_one_it_follows(self, conn):
        ctx = decided_build(conn)
        a = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[POSTMARK])
        b = q.lock_architecture(conn, project_id=ctx.pid, approved_providers=[SENTRY])
        assert b["summary_markdown"].splitlines()[-1] == \
            f"Follows lock request: {a['approval_request_id'][:8]}"

    def test_verify_prints_each_remaining_criterion_on_one_line(
            self, cli_db, tmp_path, monkeypatch, capsys):
        import subprocess
        import sys

        def git(*args):
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        git("config", "commit.gpgsign", "false")
        (tmp_path / "ok.py").write_text("print('fine')\n")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(q, "record_executed_verification", lambda *a, **k: {
            "outcome": "pass", "requirement": "todays-bookings\nPASS: other #9",
            "criterion_ordinal": 1, "remaining": ["#2 Empty state\nPASS: todays-bookings #2"]})
        assert cli.main(["verify", "--task", "t", "--req", "todays-bookings", "--criterion", "1",
                         "--", sys.executable, "ok.py"]) == 0
        rows = [r for r in capsys.readouterr().out.splitlines() if "PASS" in r]
        assert len(rows) == 2 and rows[0].startswith("PASS: todays-bookings PASS: other #9 #1 at ")
        assert rows[1] == "  still unverified: #2 Empty state PASS: todays-bookings #2"


def decided_adopt_mit(conn):
    """One MIT candidate, decided ADOPT by CIP's rules, pinned to its only revision."""
    _, cv = make_capability(conn, license="MIT")
    ctx = scoped(conn, constraints=[LICENSE_CONSTRAINT])
    ctx.cv_id = cv
    q.search_for_requirement(conn, req_id=ctx.need_id)
    d = q.record_decision(conn, req_id=ctx.need_id)
    assert d["verdict"] == "ADOPT", d
    return ctx
