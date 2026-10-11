"""
Unit tests for core.pdr — the pure rules of the PDR-to-Build workflow.

No database. Everything here is a function of its arguments, so these
cover the decision tables exhaustively and cheaply; test_pdr_workflow.py
checks that the tools actually apply them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from core.pdr import canonical, coverage, decisions, lock, lock_check, phase, requirements, spans, tasks
from core.pdr.lock_check import LockView, TaskView
from core.pdr.spans import Span


# ---------------------------------------------------------------------------
# canonical — byte-stable hashing
# ---------------------------------------------------------------------------

class TestCanonical:

    def test_key_order_does_not_matter(self):
        a = {"b": [1, 2], "a": {"y": True, "x": None}}
        b = {"a": {"x": None, "y": True}, "b": [1, 2]}
        assert canonical.content_hash(a) == canonical.content_hash(b)
        assert canonical.canonical_json(a) == '{"a":{"x":null,"y":true},"b":[1,2]}'

    def test_list_order_does_matter(self):
        assert canonical.content_hash([1, 2]) != canonical.content_hash([2, 1])

    def test_tuples_sets_and_uuids(self):
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert canonical.canonical_json({"t": (1, 2), "s": {"b", "a"}, "u": u}) == (
            '{"s":["a","b"],"t":[1,2],"u":"12345678-1234-5678-1234-567812345678"}'
        )

    def test_floats_and_non_string_keys_are_refused(self):
        with pytest.raises(TypeError):
            canonical.content_hash({"amount": 5.0})
        with pytest.raises(TypeError):
            canonical.content_hash({1: "x"})
        with pytest.raises(TypeError):
            canonical.content_hash({"x": object()})

    def test_non_ascii_is_kept_literal(self):
        assert canonical.canonical_json({"k": "café"}) == '{"k":"café"}'

    def test_normalise_text(self):
        assert canonical.normalise_text("\ufeffa\r\nb\rc\n") == "a\nb\nc\n"

    def test_hash_is_stable_across_processes_with_random_hash_seed(self):
        """Same bar as scorecard.computed_hash (Step 9)."""
        payload = {
            "decisions": [{"need_slug": "b", "flags": ["y", "x"]},
                          {"need_slug": "a", "flags": []}],
            "scope": {"deferred": ["z", "a"], "bespoke": []},
            "unicode": "naïve — ✓",
        }
        script = textwrap.dedent("""
            import json, sys
            from core.pdr.canonical import content_hash
            from core.pdr import lock
            payload = json.loads(sys.argv[1])
            built = lock.build_lock_payload(
                project_id="p", requirements_hash="r", architecture_doc_hash=None,
                decisions=[{"need_slug": s, "flags": ["b", "a"], "satisfies": ["y", "x"],
                            "verdict": "BUILD"} for s in ("n2", "n1")],
                project_decisions=[], approved_providers=[], cost_commitments=[],
                unresolved_risks=[], scope={"deferred": ["z", "a"], "bespoke": []},
            )
            print(content_hash(payload), lock.payload_hash(built))
        """)
        outputs = set()
        for seed in ("0", "1", "random", "random"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run(
                [sys.executable, "-c", script, json.dumps(payload)],
                capture_output=True, text=True, env=env, check=True,
            )
            outputs.add(out.stdout.strip())
        assert len(outputs) == 1
        assert outputs.pop().split()[0] == canonical.content_hash(payload)


# ---------------------------------------------------------------------------
# spans — CIP locates the quote
# ---------------------------------------------------------------------------

DOC = (
    "# Title\n\nIntro paragraph.\n\n"
    "## Requirements\n1. Users must sign in.\n2. Users must sign in.\n"
    "3. Reports are exported\n   as PDF files.\n\n"
    "## Notes\n```\n# not a heading\n```\n\n"
    "## Parent\n### Child\nChild body.\n"
)


class TestSpans:

    def test_exact_match_returns_offsets_into_the_text(self):
        sp = spans.locate_quote(DOC, "Intro paragraph.")
        assert DOC[sp.start:sp.end] == "Intro paragraph."

    def test_whitespace_is_forgiven_wording_is_not(self):
        sp = spans.locate_quote(DOC, "Reports are exported as PDF files.")
        assert DOC[sp.start:sp.end] == "Reports are exported\n   as PDF files."
        assert spans.locate_quote(DOC, "Reports are exported as CSV files.") is None
        assert spans.locate_quote(DOC, "reports are exported as PDF files.") is None

    def test_case_must_match_on_a_single_line_quote_too(self):
        assert spans.locate_quote(DOC, "Intro paragraph.") is not None
        assert spans.locate_quote(DOC, "intro paragraph.") is None
        assert spans.locate_quote(DOC, "INTRO PARAGRAPH.") is None

    def test_repeated_quote_uses_the_hint(self):
        first = spans.locate_quote(DOC, "Users must sign in.")
        second = spans.locate_quote(DOC, "Users must sign in.", start_hint=first.end + 5)
        assert second.start > first.start
        assert spans.locate_quote(DOC, "Users must sign in.", start_hint=0) == first

    def test_too_short_or_empty_is_not_an_anchor(self):
        assert spans.locate_quote(DOC, "sign in") is None
        assert spans.locate_quote(DOC, "") is None
        assert spans.locate_quote("", "Users must sign in.") is None

    def test_sections_skip_fences_and_flag_heading_only_parents(self):
        secs = spans.split_sections(DOC)
        assert [s.heading for s in secs] == ["Title", "Requirements", "Notes", "Parent", "Child"]
        assert [s.has_body for s in secs] == [True, True, True, False, True]
        assert secs[0].start == 0 and secs[-1].end == len(DOC)
        for a, b in zip(secs, secs[1:]):
            assert a.end == b.start

    def test_preamble_and_plain_text_documents(self):
        secs = spans.split_sections("Lead-in text.\n\n# First\nBody.\n")
        assert [s.heading for s in secs] == ["(preamble)", "First"]
        plain = spans.split_sections("First para\nline two\n\nSecond para\n\n\nThird")
        assert [s.heading for s in plain] == ["First para", "Second para", "Third"]
        assert spans.split_sections("") == ()

    def test_coverage_lists_sections_with_no_requirement(self):
        sp = spans.locate_quote(DOC, "Reports are exported as PDF files.")
        cov = spans.span_coverage(DOC, [sp])
        assert cov["section_count"] == 4                 # 'Parent' has no body
        assert cov["mapped_section_count"] == 1
        assert [s["heading"] for s in cov["unmapped_sections"]] == ["Title", "Notes", "Child"]

    def test_coverage_ignores_invalid_spans_and_merges_overlaps(self):
        # "# Title\n" is 8 characters; these two spans cover exactly that line.
        cov = spans.span_coverage(DOC, [Span(0, 5), Span(3, 8), Span(9999, 10000), Span(7, 7)])
        assert cov["quoted_chars"] == 8
        assert cov["mapped_section_count"] == 0          # only the heading line was hit

    def test_a_span_on_the_heading_line_does_not_map_the_section(self):
        heading_only = spans.span_coverage(DOC, [Span(0, 7)])
        assert heading_only["mapped_section_count"] == 0


# ---------------------------------------------------------------------------
# requirements — validation, snapshot, readiness
# ---------------------------------------------------------------------------

TEXT = "# PDR\n\nUsers must sign in with Google.\n\nStaff see today's bookings.\n"
DRAFT = {
    "text": "Sign in", "acceptance_criteria": ["A user signs in"],
    "quote": "Users must sign in with Google.",
}


class TestRequirementDrafts:

    def test_valid_draft(self):
        clean, errors = requirements.validate_requirement_draft(DRAFT, TEXT)
        assert errors == []
        assert clean["slug"] == "sign-in" and clean["kind"] == "functional"
        assert TEXT[clean["span"]["start"]:clean["span"]["end"]] == DRAFT["quote"]

    def test_text_not_stored_is_an_error_not_a_pass(self):
        clean, errors = requirements.validate_requirement_draft(DRAFT, None)
        assert clean is None and "not stored" in errors[0]

    @pytest.mark.parametrize("patch,expect", [
        ({"text": " "}, "text is required"),
        ({"quote": None}, "quote is required"),
        ({"quote": "not in the document at all"}, "quote not found"),
        ({"quote": "sign in"}, "too short"),
        ({"priority": "urgent"}, "priority must be"),
        ({"kind": "epic"}, "kind must be"),
        ({"kind": "capability_need"}, "record_capability_needs"),
        ({"acceptance_criteria": []}, "acceptance_criteria is required"),
        ({"disposition": "via_needs"}, "cannot be set directly"),
        ({"disposition": "bespoke"}, "requires disposition_reason"),
        ({"disposition": "someday"}, "disposition must be"),
        ({"constraints": [{"kind": "license_allowlist"}]}, "constraints belong on capability needs"),
    ])
    def test_invalid_drafts(self, patch, expect):
        clean, errors = requirements.validate_requirement_draft({**DRAFT, **patch}, TEXT)
        assert clean is None
        assert any(expect in e for e in errors), errors

    def test_only_out_of_scope_requirements_may_go_without_criteria(self):
        """
        A requirement that is in the build must be verifiable, whatever its
        kind. A constraint with no criterion used to slip through, and then
        sat in scope forever with nothing that could mark it verified.
        """
        deferred = {**DRAFT, "acceptance_criteria": [], "disposition": "deferred",
                    "disposition_reason": "later"}
        not_applicable = {**DRAFT, "acceptance_criteria": [], "kind": "constraint",
                          "disposition": "not_applicable",
                          "disposition_reason": "enforced on each need"}
        in_scope_constraint = {**DRAFT, "acceptance_criteria": [], "kind": "constraint"}
        assert requirements.validate_requirement_draft(deferred, TEXT)[1] == []
        assert requirements.validate_requirement_draft(not_applicable, TEXT)[1] == []
        errors = requirements.validate_requirement_draft(in_scope_constraint, TEXT)[1]
        assert any("acceptance_criteria is required" in e for e in errors)

    def test_a_single_criterion_string_is_accepted(self):
        clean, _ = requirements.validate_requirement_draft(
            {**DRAFT, "acceptance_criteria": "A user signs in"}, TEXT)
        assert clean["acceptance_criteria"] == ["A user signs in"]

    def test_non_object_is_refused(self):
        assert requirements.validate_requirement_draft("x", TEXT)[0] is None


NEED = {"description": "OAuth sign-in", "search_terms": ["oauth", "OAuth", "sso"],
        "problem_types": ["OAuth Flow"], "satisfies": ["sign-in"]}


class TestNeedDrafts:

    def test_valid_need_normalises_terms_and_tags(self):
        clean, errors, warnings = requirements.validate_need_draft(NEED)
        assert errors == []
        assert clean["search_terms"] == ["oauth", "sso"]
        assert clean["problem_types"] == ["oauth-flow"]
        assert warnings and "LLM judgment" in warnings[0]

    def test_constraint_with_a_matcher_removes_the_warning(self):
        _, _, warnings = requirements.validate_need_draft(
            {**NEED, "constraints": [{"kind": "license_allowlist", "spdx_ids": ["MIT"]}]})
        assert warnings == []

    @pytest.mark.parametrize("patch,expect", [
        ({"description": ""}, "description is required"),
        ({"search_terms": []}, "search_terms is required"),
        ({"search_terms": ["x"]}, "characters"),
        ({"search_terms": [f"term {i}" for i in range(9)]}, "at most 8"),
        ({"satisfies": []}, "satisfies is required"),
        ({"constraints": [{"kind": "bogus"}]}, "'kind' must be one of"),
        ({"constraints": [{"kind": "license_allowlist", "spdx_ids": []}]}, "spdx_ids"),
        ({"constraints": "nope"}, "constraints must be a list"),
    ])
    def test_invalid_needs(self, patch, expect):
        clean, errors, _ = requirements.validate_need_draft({**NEED, **patch})
        assert clean is None
        assert any(expect in e for e in errors), errors

    def test_fingerprint_tracks_terms_and_constraints_only(self):
        base = requirements.need_fingerprint(search_terms=["oauth", "sso"], constraints=[])
        assert base == requirements.need_fingerprint(search_terms=["SSO", "oauth"], constraints=[])
        assert base != requirements.need_fingerprint(search_terms=["oauth"], constraints=[])
        assert base != requirements.need_fingerprint(
            search_terms=["oauth", "sso"],
            constraints=[{"kind": "license_allowlist", "detail": {"spdx_ids": ["MIT"]}}])


def _req(slug, disposition="via_needs", **over):
    return {"slug": slug, "kind": "functional", "priority": "must", "text": slug,
            "acceptance_criteria": ["works"], "disposition": disposition,
            "disposition_reason": None, "quote": f"quote for {slug}",
            "source_id": "s1", "has_span": True, **over}


def _need(slug, satisfies, **over):
    return {"slug": slug, "description": slug, "constraints": [], "satisfies": satisfies, **over}


class TestSnapshotAndReadiness:

    def test_snapshot_hash_ignores_order(self):
        a = requirements.requirements_snapshot(
            source_content_hash="h", requirements=[_req("a"), _req("b")],
            needs=[_need("n", ["b", "a"])])
        b = requirements.requirements_snapshot(
            source_content_hash="h", requirements=[_req("b"), _req("a")],
            needs=[_need("n", ["a", "b"])])
        assert requirements.requirements_hash(a) == requirements.requirements_hash(b)

    @pytest.mark.parametrize("change", [
        lambda r, n: r[0].update(text="reworded"),
        lambda r, n: r[0].update(acceptance_criteria=["works", "and more"]),
        lambda r, n: r[0].update(disposition="bespoke", disposition_reason="x"),
        lambda r, n: r[0].update(quote="a different quote"),
        lambda r, n: n[0].update(satisfies=[]),
        lambda r, n: n[0].update(constraints=[{"kind": "license_allowlist",
                                               "detail": {"spdx_ids": ["MIT"]}}]),
    ])
    def test_snapshot_hash_moves_when_the_scope_does(self, change):
        reqs, needs = [_req("a")], [_need("n", ["a"])]
        before = requirements.requirements_hash(requirements.requirements_snapshot(
            source_content_hash="h", requirements=reqs, needs=needs))
        change(reqs, needs)
        after = requirements.requirements_hash(requirements.requirements_snapshot(
            source_content_hash="h", requirements=reqs, needs=needs))
        assert before != after

    def test_a_new_pdr_text_moves_the_hash(self):
        args = dict(requirements=[_req("a")], needs=[_need("n", ["a"])])
        assert requirements.requirements_hash(
            requirements.requirements_snapshot(source_content_hash="h1", **args)
        ) != requirements.requirements_hash(
            requirements.requirements_snapshot(source_content_hash="h2", **args))

    def test_ready_scope_has_no_blockers(self):
        assert requirements.confirmation_blockers(
            latest_source_id="s1",
            requirements=[_req("a"), _req("b", "bespoke"), _req("c", "deferred")],
            needs=[_need("n", ["a"])]) == []

    @pytest.mark.parametrize("reqs,needs,source,expect", [
        ([], [], "s1", "no requirements recorded"),
        ([_req("a")], [_need("n", ["a"])], None, "no PDR has been ingested"),
        ([_req("a", None)], [], "s1", "no disposition"),
        ([_req("a")], [], "s1", "no active capability need linked"),
        ([_req("a", "bespoke")], [_need("n", [])], "s1", "linked to no active requirement"),
        ([_req("a", has_span=False)], [_need("n", ["a"])], "s1", "no source quote"),
        ([_req("a", source_id="old")], [_need("n", ["a"])], "s1", "superseded PDR"),
        ([_req("a", acceptance_criteria=[])], [_need("n", ["a"])], "s1", "no acceptance criteria"),
    ])
    def test_blockers(self, reqs, needs, source, expect):
        out = requirements.confirmation_blockers(
            latest_source_id=source, requirements=reqs, needs=needs)
        assert any(expect in b for b in out), out


# ---------------------------------------------------------------------------
# decisions — who decided
# ---------------------------------------------------------------------------

def classify(**over):
    args = dict(
        constraint_kinds=[], computed_verdict="BUILD", computed_capability_version_id=None,
        proposed_verdict=None, proposed_capability_version_id=None,
        evaluated_capability_version_ids=[], blocking_gap_capability_version_ids=[],
        evaluation_hit_count=0, rationale="",
    )
    args.update(over)
    return decisions.classify_decision(**args)


INFORMATIVE = dict(constraint_kinds=["license_allowlist"], evaluated_capability_version_ids=["a", "b"])


class TestDecisionAuthority:

    def test_fit_is_informative_only_with_a_matched_constraint(self):
        assert decisions.fit_is_informative(["license_allowlist"])
        assert decisions.fit_is_informative(["x", "required_interface"])
        assert not decisions.fit_is_informative([])
        assert not decisions.fit_is_informative(["general"])

    @pytest.mark.parametrize("evaluated,hits,expect", [
        (1, 0, "candidates_evaluated"), (1, 3, "candidates_evaluated"),
        (0, 2, "banked_only"), (0, 0, "no_registry_coverage"),
    ])
    def test_coverage_outcome(self, evaluated, hits, expect):
        assert decisions.coverage_outcome(
            evaluated_count=evaluated, evaluation_hit_count=hits) == expect

    def test_no_candidates_forces_build_by_cip(self):
        out = classify()
        assert (out.final_verdict, out.decided_by) == ("BUILD", "cip_rules")
        assert out.flags == ("cold_start_build",)
        assert out.coverage_outcome == "no_registry_coverage"

    def test_no_candidates_but_a_banked_match(self):
        out = classify(evaluation_hit_count=1)
        assert out.coverage_outcome == "banked_only"
        assert out.flags == ("build_despite_banked_match",)

    def test_uninformative_fit_needs_a_proposal(self):
        out = classify(computed_verdict="ADOPT", computed_capability_version_id="a",
                       evaluated_capability_version_ids=["a"])
        assert isinstance(out, decisions.DecisionRefusal)
        assert "cannot compute a verdict" in out.reason

    def test_uninformative_fit_records_llm_judgment_and_hides_the_computed_verdict(self):
        out = classify(computed_verdict="ADOPT", computed_capability_version_id="a",
                       evaluated_capability_version_ids=["a"],
                       proposed_verdict="adopt", proposed_capability_version_id="a",
                       rationale="fits")
        assert out.decided_by == "llm_judgment"
        assert out.computed_verdict is None and out.computed_capability_version_id is None
        assert out.final_capability_version_id == "a"

    def test_informative_fit_without_a_proposal_takes_cips_verdict(self):
        out = classify(**INFORMATIVE, computed_verdict="ADAPT",
                       computed_capability_version_id="b")
        assert (out.final_verdict, out.final_capability_version_id, out.decided_by) == (
            "ADAPT", "b", "cip_rules")

    def test_agreement_is_cip_rules_and_needs_no_rationale(self):
        out = classify(**INFORMATIVE, computed_verdict="ADOPT",
                       computed_capability_version_id="a",
                       proposed_verdict="ADOPT", proposed_capability_version_id="a")
        assert out.decided_by == "cip_rules"

    @pytest.mark.parametrize("proposed,cv", [("BUILD", None), ("ADAPT", "a"), ("ADOPT", "b")])
    def test_any_difference_is_an_override(self, proposed, cv):
        args = dict(**INFORMATIVE, computed_verdict="ADOPT", computed_capability_version_id="a",
                    proposed_verdict=proposed, proposed_capability_version_id=cv)
        assert isinstance(classify(**args), decisions.DecisionRefusal)       # no rationale
        out = classify(**args, rationale="because")
        assert out.decided_by == "llm_override" and out.computed_verdict == "ADOPT"

    def test_computed_reject_means_build_for_a_need(self):
        out = classify(**INFORMATIVE, computed_verdict="REJECT")
        assert (out.final_verdict, out.decided_by) == ("BUILD", "cip_rules")
        assert out.computed_verdict == "REJECT" and "rejected every candidate" in out.note
        agree = classify(**INFORMATIVE, computed_verdict="REJECT", proposed_verdict="BUILD")
        assert agree.decided_by == "cip_rules"

    def test_reject_and_unknown_verdicts_are_refused(self):
        assert "retire" in classify(proposed_verdict="REJECT").reason
        assert "Invalid verdict" in classify(proposed_verdict="MAYBE").reason
        assert isinstance(classify(computed_verdict="???"), decisions.DecisionRefusal)

    def test_reuse_must_name_an_evaluated_candidate(self):
        base = dict(**INFORMATIVE, computed_verdict="BUILD", rationale="x")
        assert "requires chosen_capability_version_id" in classify(
            **base, proposed_verdict="ADOPT").reason
        assert "was not evaluated" in classify(
            **base, proposed_verdict="ADOPT", proposed_capability_version_id="zzz").reason
        assert "does not take" in classify(
            constraint_kinds=["license_allowlist"], evaluated_capability_version_ids=["a"],
            computed_verdict="ADOPT", computed_capability_version_id="a",
            proposed_verdict="BUILD", proposed_capability_version_id="a",
            rationale="x").reason

    def test_overriding_a_blocking_gap_is_flagged(self):
        out = classify(**INFORMATIVE, computed_verdict="REJECT",
                       proposed_verdict="ADAPT", proposed_capability_version_id="b",
                       blocking_gap_capability_version_ids=["b"], rationale="internal use")
        assert out.flags == ("overrides_blocking_gap",) and out.note == ""

    def test_a_metadata_only_winner_is_not_a_cip_decision(self):
        """The fit matchers read evidence. No evidence, no verdict."""
        base = dict(**INFORMATIVE, computed_verdict="REJECT",
                    winner_capability_version_id="a",
                    unanalysed_capability_version_ids=["a", "b"])
        refused = classify(**base)
        assert isinstance(refused, decisions.DecisionRefusal)
        assert "holds no analysed evidence" in refused.reason
        assert "Supply a verdict" in refused.reason      # the tool's argument is `verdict`
        out = classify(**base, proposed_verdict="ADOPT", proposed_capability_version_id="a",
                       rationale="MIT per the repo")
        assert out.decided_by == "llm_judgment" and out.computed_verdict is None
        # Missing evidence is reported as that, not as a gap someone overrode.
        assert out.flags == ("unanalysed_candidate", decisions.WHY_INSUFFICIENT)
        assert "no analysed evidence" in out.note

    def test_an_analysed_winner_decides_even_if_other_candidates_are_not_analysed(self):
        base = dict(**INFORMATIVE, computed_verdict="ADOPT", computed_capability_version_id="a",
                    winner_capability_version_id="a", unanalysed_capability_version_ids=["b"])
        assert classify(**base).decided_by == "cip_rules"
        over = classify(**base, proposed_verdict="ADOPT", proposed_capability_version_id="b",
                        rationale="smaller")
        assert over.decided_by == "llm_override" and over.flags == ("unanalysed_candidate",)

    def test_build_with_only_metadata_candidates_still_needs_a_proposal(self):
        """CIP's 'BUILD' here would only mean 'I could not read the candidates'."""
        base = dict(**INFORMATIVE, computed_verdict="REJECT", winner_capability_version_id="a",
                    unanalysed_capability_version_ids=["a", "b"])
        assert isinstance(classify(**base), decisions.DecisionRefusal)
        out = classify(**base, proposed_verdict="BUILD", rationale="none of them fit")
        assert (out.final_verdict, out.decided_by) == ("BUILD", "llm_judgment")
        assert out.flags == (decisions.WHY_INSUFFICIENT,)

    def test_build_with_candidates_is_not_a_cold_start(self):
        out = classify(**INFORMATIVE, computed_verdict="REJECT")
        assert out.flags == () and out.coverage_outcome == "candidates_evaluated"


# ---------------------------------------------------------------------------
# lock — payload, flags, diff
# ---------------------------------------------------------------------------

def _decision(slug="auth", verdict="ADOPT", **over):
    base = {
        "need_id": f"id-{slug}", "need_slug": slug, "decision_record_id": f"dr-{slug}",
        "verdict": verdict,
        "capability_version_id": None if verdict == "BUILD" else f"cv-{slug}",
        "capability_key": None if verdict == "BUILD" else f"pypi:{slug}",
        "pinned_revision": None if verdict == "BUILD" else "abc123",
        "pinned_source_asset": None if verdict == "BUILD" else f"o/{slug}",
        "decided_by": "cip_rules", "coverage_outcome": "candidates_evaluated",
        "flags": [], "license": None if verdict == "BUILD" else "MIT",
        "cost_tier": None if verdict == "BUILD" else "free", "satisfies": ["r1"],
    }
    base.update(over)
    return base


def _payload(**over):
    args = dict(
        project_id="p", requirements_hash="rh", architecture_doc_hash="ah",
        decisions=[_decision()], project_decisions=[], approved_providers=[],
        cost_commitments=[], unresolved_risks=[],
        scope={"in_scope": ["r1"], "bespoke": [], "deferred": [], "not_applicable": []},
    )
    args.update(over)
    return lock.build_lock_payload(**args)


def codes(payload):
    return [f["code"] for f in lock.lock_flags(payload)]


class TestLockInputs:

    def test_valid_inputs_are_normalised_and_sorted(self):
        providers, errors = lock.validate_providers([
            {"name": "Stripe", "category": "PAYMENT", "cost": "usage", "data_leaves_infra": True},
            {"name": "google", "category": "auth", "data_leaves_infra": False},
        ])
        assert errors == []
        assert [p["name"] for p in providers] == ["google", "Stripe"]
        assert providers[0]["cost"] == "free" and providers[1]["category"] == "payment"

    @pytest.mark.parametrize("bad,expect", [
        ("x", "must be a list"),
        ([{"category": "auth", "data_leaves_infra": True}], "name is required"),
        ([{"name": "A", "category": "quantum", "data_leaves_infra": True}], "category must be"),
        ([{"name": "A", "category": "auth", "cost": "lots", "data_leaves_infra": True}], "cost must be"),
        ([{"name": "A", "category": "auth"}], "data_leaves_infra"),
        ([{"name": "A", "category": "auth", "data_leaves_infra": True},
          {"name": "a", "category": "auth", "data_leaves_infra": True}], "duplicate"),
    ])
    def test_bad_providers(self, bad, expect):
        clean, errors = lock.validate_providers(bad)
        assert clean == [] and any(expect in e for e in errors), errors

    def test_costs_decisions_and_risks(self):
        costs, e = lock.validate_cost_commitments(
            [{"item": "Hosting", "kind": "recurring", "amount": 5, "currency": "usd", "period": "Month"}])
        assert e == [] and costs[0] == {"item": "Hosting", "kind": "recurring", "amount": "5",
                                        "currency": "USD", "period": "month"}
        assert lock.validate_cost_commitments([{"item": "", "kind": "x"}])[1]
        decs, e = lock.validate_project_decisions(
            [{"key": "Database", "choice": "Postgres"}, {"key": "auth", "category": "auth_model",
                                                         "choice": "sessions"}])
        assert e == [] and [d["key"] for d in decs] == ["auth", "database"]
        assert lock.validate_project_decisions([{"key": "db"}])[1]
        assert lock.validate_project_decisions([{"key": "a", "choice": "x"}, {"key": "A", "choice": "y"}])[1]
        assert lock.validate_risks([" b ", "a", "a"]) == (["a", "b"], [])
        assert lock.validate_risks("oops")[1]


class TestLockPayload:

    def test_payload_hash_ignores_input_order(self):
        a = _payload(decisions=[_decision("a"), _decision("b", flags=["y", "x"])])
        b = _payload(decisions=[_decision("b", flags=["x", "y"]), _decision("a")])
        assert lock.payload_hash(a) == lock.payload_hash(b)

    @pytest.mark.parametrize("over", [
        {"requirements_hash": "other"},
        {"architecture_doc_hash": "other"},
        {"decisions": [_decision(verdict="BUILD")]},
        {"decisions": [_decision(pinned_revision="def456")]},
        {"project_decisions": [{"key": "db", "category": "database", "choice": "pg", "rationale": ""}]},
        {"approved_providers": [{"name": "G", "category": "auth", "purpose": "", "cost": "free",
                                 "data_leaves_infra": False}]},
        {"unresolved_risks": ["something"]},
        {"scope": {"in_scope": ["r1"], "bespoke": ["r2"], "deferred": [], "not_applicable": []}},
    ])
    def test_payload_hash_covers_everything_approved(self, over):
        assert lock.payload_hash(_payload()) != lock.payload_hash(_payload(**over))

    def test_clean_payload_has_no_flags(self):
        assert codes(_payload()) == []

    @pytest.mark.parametrize("decision,expect", [
        (_decision(decided_by="llm_override"), "llm_override"),
        (_decision(decided_by="llm_judgment"), "llm_judgment"),
        (_decision(flags=["overrides_blocking_gap"]), "overrides_blocking_gap"),
        (_decision(verdict="BUILD", flags=["cold_start_build"]), "cold_start_build"),
        (_decision(verdict="BUILD", flags=["build_despite_banked_match"]), "build_despite_banked_match"),
        (_decision(license="GPL-3.0"), "non_permissive_license"),
        (_decision(license=None), "unknown_license"),
        (_decision(flags=["unanalysed_candidate"]), "unanalysed_candidate"),
        (_decision(cost_tier="paid"), "paid_capability"),
    ])
    def test_decision_flags(self, decision, expect):
        assert expect in codes(_payload(decisions=[decision]))

    def test_reference_with_unknown_license_is_not_a_dependency_flag(self):
        assert "unknown_license" not in codes(
            _payload(decisions=[_decision(verdict="REFERENCE", license=None)]))

    def test_provider_cost_risk_and_scope_flags(self):
        payload = _payload(
            architecture_doc_hash=None,
            approved_providers=[{"name": "Stripe", "category": "payment", "purpose": "",
                                 "cost": "usage", "data_leaves_infra": True}],
            cost_commitments=[{"item": "Hosting", "kind": "recurring", "amount": "5",
                               "currency": "USD", "period": "month"}],
            unresolved_risks=["open question"],
            scope={"in_scope": ["r1", "r2"], "bespoke": ["r2"], "deferred": [], "not_applicable": []},
        )
        assert set(codes(payload)) == {
            "sensitive_provider", "paid_provider", "data_leaves_infra", "cost_commitment",
            "unresolved_risk", "bespoke_requirements", "no_architecture_doc"}

    def test_flags_are_ordered_most_serious_first(self):
        payload = _payload(
            architecture_doc_hash=None,
            decisions=[_decision(decided_by="llm_judgment", flags=["overrides_blocking_gap"])])
        assert codes(payload) == ["overrides_blocking_gap", "llm_judgment", "no_architecture_doc"]

    def test_first_lock_has_no_diff(self):
        assert lock.diff_payloads(None, _payload()) == {"is_first_lock": True, "changes": []}

    def test_diff_names_what_changed(self):
        old = _payload(
            decisions=[_decision("auth"), _decision("pdf")],
            approved_providers=[{"name": "Google", "category": "auth", "purpose": "",
                                 "cost": "free", "data_leaves_infra": True}])
        new = _payload(
            requirements_hash="rh2",
            decisions=[_decision("auth", verdict="BUILD"), _decision("queue")],
            project_decisions=[{"key": "db", "category": "database", "choice": "pg", "rationale": ""}])
        kinds = {(c["kind"], c["subject"]) for c in lock.diff_payloads(old, new)["changes"]}
        assert kinds == {
            ("decision_changed", "auth"), ("decision_removed", "pdf"),
            ("decision_added", "queue"), ("project_decision_added", "db"),
            ("provider_removed", "google"), ("requirements_changed", "requirements")}

    def test_who_decided_shows_in_the_diff(self):
        """
        The same ADOPT moving from CIP's rules to an LLM judgment is a
        change the human should see. And the same outcome decided again is
        still a different lock: it is itemised, never reported as "none".
        """
        old = _payload(decisions=[_decision(decided_by="cip_rules", decision_record_id="dr1")])
        new = _payload(decisions=[_decision(decided_by="llm_judgment", decision_record_id="dr2")])
        (change,) = lock.diff_payloads(old, new)["changes"]
        assert change["kind"] == "decision_changed"
        assert "cip_rules" in change["detail"] and "llm_judgment" in change["detail"]

        again = _payload(decisions=[_decision(decision_record_id="dr2")])
        base = _payload(decisions=[_decision(decision_record_id="dr1")])
        assert [c["kind"] for c in lock.diff_payloads(base, again)["changes"]] == \
            ["decision_rerecorded"]

    def test_summary_puts_changes_and_flags_first(self):
        payload = _payload(decisions=[_decision(decided_by="llm_override")])
        text = lock.render_lock_summary(
            project_name="Clinic", next_version=2, payload=payload,
            flags=lock.lock_flags(payload),
            diff={"is_first_lock": False, "changes": [
                {"kind": "decision_changed", "subject": "auth", "detail": "BUILD -> ADOPT"}]})
        assert text.index("Changes from lock v1") < text.index("Needs your attention") \
            < text.index("## Decisions")
        assert "[llm_override] auth - " in text and "@abc123" in text


# ---------------------------------------------------------------------------
# lock_check — structured, fail-closed
# ---------------------------------------------------------------------------

LOCK = LockView(
    version=3, stale_reasons=(),
    decisions={
        "auth": {"need_id": "n-auth", "verdict": "ADOPT", "capability_version_id": "cv1",
                 "capability_key": "pypi:authlib", "pinned_revision": "abc"},
        "reports": {"need_id": "n-rep", "verdict": "BUILD", "capability_version_id": None,
                    "capability_key": None, "pinned_revision": None},
    },
    approved_providers=({"name": "Google", "category": "auth", "cost": "free"},),
)
TASK = TaskView(id="t1", title="Sign-in", status="in_progress", is_current=True,
                permitted_paths=("src/auth/", "tests/test_auth*.py"),
                prohibited_changes=("do not change the session schema",))


def check(change, lock_view=LOCK, task=TASK, task_requested=True):
    return lock_check.evaluate_change(
        lock=lock_view, task=task, task_requested=task_requested, change=change)


class TestLockCheck:

    def test_no_lock_blocks(self):
        assert check({"changed_paths": ["src/auth/x.py"]}, lock_view=None).verdict == "blocked"

    @pytest.mark.parametrize("change", [{}, {"description": "refactor"}, "refactor", None,
                                        {"changed_paths": []}])
    def test_nothing_checkable_blocks(self, change):
        assert check(change).verdict == "blocked"

    def test_allowed_change_returns_the_tasks_reminders(self):
        r = check({"changed_paths": ["src/auth/login.py", "tests/test_auth_login.py"]})
        assert r.verdict == "allowed" and "lock v3" in r.reason
        assert r.reminders == ("do not change the session schema",)

    @pytest.mark.parametrize("path", ["src/billing/x.py", "src/authx/y.py", "/etc/passwd",
                                      "src/auth/../../secrets", "C:\\Windows\\x", "tests/other.py"])
    def test_paths_outside_the_task(self, path):
        assert check({"changed_paths": [path]}).verdict == "needs_approval"

    def test_paths_need_a_known_current_task_with_paths(self):
        assert "needs build_task_id" in check(
            {"changed_paths": ["src/auth/x.py"]}, task=None, task_requested=False).reason
        assert "does not match a task" in check(
            {"changed_paths": ["src/auth/x.py"]}, task=None, task_requested=True).reason
        stale = TaskView("t", "Old", "in_progress", False, ("src/",), ())
        assert "superseded lock" in check({"changed_paths": ["src/a.py"]}, task=stale).reason
        done = TaskView("t", "Done", "verified", True, ("src/",), ())
        assert check({"changed_paths": ["src/a.py"]}, task=done).verdict == "needs_approval"
        bare = TaskView("t", "Bare", "planned", True, (), ())
        assert "no permitted_paths" in check({"changed_paths": ["src/a.py"]}, task=bare).reason

    def test_capability_changes(self):
        same = check({"capability_changes": [{"need": "auth", "capability_version_id": "cv1"}]})
        by_id = check({"capability_changes": [{"need": "n-auth", "capability_version_id": "cv1",
                                               "verdict": "adopt"}]})
        swap = check({"capability_changes": [{"need": "auth", "capability_version_id": "cv2"}]})
        to_reuse = check({"capability_changes": [{"need": "reports", "capability_version_id": "cv9"}]})
        verdict = check({"capability_changes": [{"need": "auth", "capability_version_id": "cv1",
                                                 "verdict": "WRAP"}]})
        unknown = check({"capability_changes": [{"need": "queue", "capability_version_id": "cv3"}]})
        assert same.verdict == by_id.verdict == "allowed"
        assert swap.verdict == "needs_approval" and swap.cited[0]["need_slug"] == "auth"
        assert "pypi:authlib" in swap.reason
        assert to_reuse.verdict == verdict.verdict == unknown.verdict == "needs_approval"
        assert "not a locked capability need" in unknown.reason
        assert check({"capability_changes": [{"capability_version_id": "x"}]}).verdict == "needs_approval"

    def test_provider_changes(self):
        assert check({"provider_changes": [{"name": "google", "action": "use"}]}).verdict == "allowed"
        add = check({"provider_changes": [{"name": "Auth0"}]})
        assert add.verdict == "needs_approval" and "Google" in add.reason
        drop = check({"provider_changes": [{"name": "Google", "action": "remove"}]})
        assert drop.verdict == "needs_approval" and drop.cited[0]["type"] == "provider"
        assert check({"provider_changes": [{"name": "Nope", "action": "remove"}]}).verdict == "needs_approval"
        assert check({"provider_changes": [{"name": "Google", "action": "dance"}]}).verdict == "needs_approval"
        assert check({"provider_changes": [{}]}).verdict == "needs_approval"

    def test_dependency_adds(self):
        assert check({"dependency_adds": [{"ecosystem": "PyPI", "name": "AuthLib"}]}).verdict == "allowed"
        r = check({"dependency_adds": [{"ecosystem": "pypi", "name": "requests"}]})
        assert r.verdict == "needs_approval" and "pypi:requests" in r.reason
        assert check({"dependency_adds": [{"ecosystem": "pypi"}]}).verdict == "needs_approval"

    def test_a_stale_lock_never_allows(self):
        stale = LockView(3, ("the requirements have changed since the lock",), LOCK.decisions,
                         LOCK.approved_providers)
        r = check({"changed_paths": ["src/auth/login.py"]}, lock_view=stale)
        assert r.verdict == "needs_approval" and "stale" in r.reason

    def test_every_problem_is_reported_not_just_the_first(self):
        r = check({"changed_paths": ["src/billing/x.py"],
                   "provider_changes": [{"name": "Auth0"}],
                   "dependency_adds": [{"ecosystem": "pypi", "name": "requests"}]})
        assert len(r.reasons) == 3

    @pytest.mark.parametrize("path,patterns,expect", [
        ("src/auth/login.py", ["src/auth/"], True),
        ("src/auth/login.py", ["src/auth"], True),
        ("src/auth", ["src/auth/"], True),
        ("src/authx/a.py", ["src/auth"], False),
        ("src/a/b/c.py", ["src/**/c.py"], True),
        ("tests/test_auth.py", ["tests/test_*.py"], True),
        ("README.md", ["./README.md"], True),
        ("src/a.py", ["", "/"], False),
        ("src/a.py", ["SRC/"], False),
    ])
    def test_path_matching(self, path, patterns, expect):
        assert lock_check.path_permitted(path, patterns) is expect

    def test_normalise_path(self):
        assert lock_check.normalise_path(".\\src\\a.py") == "src/a.py"
        for bad in ("", "/abs", "C:/x", "a/../b", "..", "."):
            assert lock_check.normalise_path(bad) is None


# ---------------------------------------------------------------------------
# tasks — gate, transitions, verification
# ---------------------------------------------------------------------------

def row(slug, kind="functional", disposition="via_needs", needs=(), status="active"):
    return tasks.LinkedRow(id=f"id-{slug}", slug=slug, kind=kind, status=status,
                           disposition=None if kind == "capability_need" else disposition,
                           need_slugs=tuple(needs))


LOCKED = {
    "auth": {"decision_record_id": "dr1", "verdict": "ADOPT", "capability_version_id": "cv1"},
    "reports": {"decision_record_id": "dr2", "verdict": "BUILD", "capability_version_id": None},
    "notes": {"decision_record_id": "dr3", "verdict": "REFERENCE", "capability_version_id": "cv3"},
}
CURRENT = {"auth": "dr1", "reports": "dr2", "notes": "dr3"}


def gate(rows, **over):
    args = dict(
        has_active_lock=True, lock_stale_reasons=[], locked_decisions=LOCKED,
        current_decision_ids=CURRENT, rows=rows, reused_capability_version_id=None,
        permitted_paths=["src/"], build_gate={"reports": (True, "ok")},
    )
    args.update(over)
    return tasks.task_gate(**args)


class TestTaskGate:

    def test_bespoke_requirement_needs_only_a_lock(self):
        assert gate([row("screen", disposition="bespoke")]) == []

    def test_no_lock_or_stale_lock(self):
        assert "No active architecture lock" in gate(
            [row("screen", disposition="bespoke")], has_active_lock=False)[0]
        assert "stale" in gate([row("screen", disposition="bespoke")],
                               lock_stale_reasons=["requirements changed"])[0]

    def test_build_need_requires_step_13(self):
        rows = [row("export", needs=["reports"]), row("reports", kind="capability_need")]
        assert gate(rows) == []
        refused = gate(rows, build_gate={"reports": (False, "not authorized")})
        assert "Search-before-build gate refuses 'reports': not authorized" in refused[0]
        assert gate(rows, build_gate={})                    # no result is a refusal

    def test_reuse_need_pins_the_version(self):
        rows = [row("login", needs=["auth"]), row("auth", kind="capability_need")]
        assert gate(rows, reused_capability_version_id="cv1") == []
        assert "is not what the lock pinned" in gate(
            rows, reused_capability_version_id="other")[0]
        # Leaving it out is fine: the lock already says which version, and
        # CIP fills it in rather than trusting the agent to echo it back.
        assert gate(rows) == []
        assert tasks.resolve_reused_version(rows, LOCKED, None) == "cv1"

    def test_the_pin_holds_when_the_need_is_reached_through_a_requirement(self):
        """
        Linking only the requirement used to skip the pin: the check looked
        at directly linked needs, so a task on 'login' could name any
        version it liked.
        """
        rows = [row("login", needs=["auth"])]
        assert tasks.dependent_need_slugs(rows) == ["auth"]
        assert tasks.resolve_reused_version(rows, LOCKED, None) == "cv1"
        assert "is not what the lock pinned" in gate(
            rows, reused_capability_version_id="other")[0]

    def test_reference_need_needs_no_pin_and_no_step_13(self):
        rows = [row("memo", needs=["notes"]), row("notes", kind="capability_need")]
        assert gate(rows) == []

    def test_requirement_alone_still_checks_its_needs(self):
        assert gate([row("export", needs=["reports"])]) == []
        assert "not in the active lock" in gate([row("export", needs=["queue"])])[0]
        drift = gate([row("export", needs=["reports"])],
                     current_decision_ids={**CURRENT, "reports": "dr9"})
        assert "has changed since it was locked" in drift[0]

    @pytest.mark.parametrize("rows,expect", [
        ([], "at least one requirement"),
        ([row("auth", kind="capability_need")], "at least one PDR requirement"),
        ([row("later", disposition="deferred")], "is deferred"),
        ([row("na", disposition="not_applicable")], "is not_applicable"),
        ([row("open", disposition=None)], "no disposition"),
        ([row("orphan", needs=[])], "has no capability need"),
        ([row("gone", disposition="bespoke", status="retired")], "is retired"),
    ])
    def test_refusals(self, rows, expect):
        assert any(expect in r for r in gate(rows)), gate(rows)

    def test_paths_are_required_and_must_be_relative(self):
        rows = [row("screen", disposition="bespoke")]
        assert "permitted_paths is required" in gate(rows, permitted_paths=[])[0]
        assert "permitted_paths is required" in gate(rows, permitted_paths=None)[0]
        assert "repo-relative" in gate(rows, permitted_paths=["../x"])[0]
        assert "repo-relative" in gate(rows, permitted_paths=["/abs"])[0]

    def test_a_task_may_rest_on_several_reused_capabilities(self):
        """
        One requirement can need two reused capabilities, and the task that
        delivers it has to link the requirement — so it cannot be split.
        Any version named must be one the lock pinned; with more than one
        there is no single answer to fill in.
        """
        locked = {**LOCKED, "pdf": {"decision_record_id": "dr4", "verdict": "WRAP",
                                    "capability_version_id": "cv4"}}
        current = {**CURRENT, "pdf": "dr4"}
        rows = [row("r", needs=["auth", "pdf"])]
        for cv in ("cv1", "cv4", None):
            assert gate(rows, locked_decisions=locked, current_decision_ids=current,
                        reused_capability_version_id=cv) == []
        assert "is not what the lock pinned" in gate(
            rows, locked_decisions=locked, current_decision_ids=current,
            reused_capability_version_id="cv3")[0]
        assert tasks.resolve_reused_version(rows, locked, None) is None
        assert tasks.resolve_reused_version(rows, locked, "cv4") == "cv4"

    def test_reused_version_without_a_reuse_decision_under_the_task(self):
        rows = [row("screen", disposition="bespoke")]
        for cv in ("cv1", "cvX"):
            assert "does not belong to a reuse decision this task rests on" in gate(
                rows, reused_capability_version_id=cv)[0]
        # REFERENCE is not a dependency: nothing to pin, nothing to fill.
        ref = [row("memo", needs=["notes"])]
        assert tasks.resolve_reused_version(ref, LOCKED, None) is None
        assert gate(ref, reused_capability_version_id="cv3")


class TestTransitionsAndVerification:

    @pytest.mark.parametrize("old,new,ok", [
        ("planned", "in_progress", True), ("planned", "abandoned", True),
        ("planned", "implemented", False), ("planned", "verified", False),
        ("in_progress", "implemented", True), ("in_progress", "planned", True),
        ("in_progress", "verified", False),
        ("implemented", "verified", True), ("implemented", "in_progress", True),
        ("verified", "implemented", True), ("verified", "in_progress", False),
        ("abandoned", "planned", False), ("abandoned", "abandoned", True),
        ("planned", "finished", False),
    ])
    def test_transitions(self, old, new, ok):
        assert (tasks.check_transition(old, new) is None) is ok

    def test_pass_needs_full_evidence(self):
        good = {"criterion_id": "c1", "outcome": "pass", "commit_sha": "abc1234",
                "test_ref": "t.py::t", "command": "pytest t.py", "exit_code": 0}
        clean, errors = tasks.validate_verification(good)
        assert errors == [] and clean["exit_code"] == 0
        for missing in ("commit_sha", "test_ref", "command", "exit_code"):
            assert tasks.validate_verification({k: v for k, v in good.items() if k != missing})[1]
        assert tasks.validate_verification({**good, "exit_code": 1})[1]
        assert tasks.validate_verification({**good, "exit_code": True})[1]
        assert tasks.validate_verification({**good, "commit_sha": "abc"})[1]

    def test_fail_and_not_run_need_only_the_criterion(self):
        assert tasks.validate_verification({"criterion_id": "c1", "outcome": "fail"})[1] == []
        clean, _ = tasks.validate_verification(
            {"req_id": "r", "criterion_ordinal": 2, "outcome": "not_run"})
        assert clean["req_ref"] == "r" and clean["criterion_ordinal"] == 2

    def test_verification_must_name_a_criterion_and_a_known_outcome(self):
        assert tasks.validate_verification({"outcome": "fail"})[1]
        assert tasks.validate_verification({"req_id": "r", "outcome": "fail"})[1]
        assert tasks.validate_verification({"criterion_id": "c", "outcome": "ok"})[1]
        assert tasks.validate_verification("pass")[1]

    def test_verified_requires_every_criterion_to_pass_latest(self):
        crit = [{"id": "c1", "requirement_slug": "r", "ordinal": 1, "text": "one"},
                {"id": "c2", "requirement_slug": "r", "ordinal": 2, "text": "two"}]
        ok = {"c1": {"outcome": "pass", "build_task_id": "t"},
              "c2": {"outcome": "pass", "build_task_id": "other"}}
        assert tasks.verified_refusal(task_id="t", criteria=crit, latest=ok) is None
        assert "r #2 (no verification recorded): two" in tasks.verified_refusal(
            task_id="t", criteria=crit, latest={"c1": ok["c1"]})
        assert "r #2 (fail)" in tasks.verified_refusal(
            task_id="t", criteria=crit, latest={**ok, "c2": {"outcome": "fail", "build_task_id": "t"}})
        assert "against this task" in tasks.verified_refusal(
            task_id="nobody", criteria=crit, latest=ok)
        assert "no acceptance criteria" in tasks.verified_refusal(
            task_id="t", criteria=[], latest={})


# ---------------------------------------------------------------------------
# coverage + phase
# ---------------------------------------------------------------------------

def cov_req(slug, disposition, needs=(), criteria=("c",), latest=None, tasks_=()):
    return {"id": f"id-{slug}", "slug": slug, "kind": "functional", "priority": "must",
            "disposition": disposition, "need_slugs": list(needs),
            "criteria": [{"id": f"{slug}-{c}", "ordinal": i + 1} for i, c in enumerate(criteria)],
            "latest": latest or {}, "task_ids": list(tasks_)}


def cov_need(slug, verdict=None, decided_by="cip_rules", outcome="candidates_evaluated"):
    decision = None if verdict is None else {
        "id": f"dr-{slug}", "final_verdict": verdict, "decided_by": decided_by,
        "coverage_outcome": outcome}
    return {"id": f"id-{slug}", "slug": slug, "searched": verdict is not None, "decision": decision}


class TestCoverageMath:

    def test_denominator_is_in_scope_requirements(self):
        out = coverage.compute_coverage(
            span_coverage=None,
            requirements=[cov_req("a", "bespoke"), cov_req("b", "deferred"),
                          cov_req("c", "not_applicable"), cov_req("d", None),
                          cov_req("e", "via_needs", needs=["n"])],
            needs=[cov_need("n")], tasks=[], lock=None)
        assert out["requirements"]["total"] == 5 and out["progress"]["in_scope"] == 2
        assert out["total_requirements"] == 2
        assert out["requirements"]["by_disposition"]["undispositioned"] == 1
        assert out["progress"]["decided"] == 1            # the bespoke one
        assert out["progress"]["locked"] == 0 and out["pdr"]["unmapped_sections"] == []

    def test_decided_locked_and_drift(self):
        reqs = [cov_req("e", "via_needs", needs=["n1", "n2"])]
        needs = [cov_need("n1", "ADOPT"), cov_need("n2", "BUILD", outcome="no_registry_coverage")]
        lock_ok = {"version": 1, "is_usable": True, "stale_reasons": [], "decisions": {
            "n1": {"decision_record_id": "dr-n1"}, "n2": {"decision_record_id": "dr-n2"}}}
        out = coverage.compute_coverage(span_coverage=None, requirements=reqs, needs=needs,
                                        tasks=[], lock=lock_ok)
        assert out["progress"]["decided"] == 1 and out["progress"]["locked"] == 1
        assert out["by_requirement"][0]["verdict"] == "MIXED"
        assert out["needs"]["reuse_pct"] == 50 and out["needs"]["registry_answered_pct"] == 50

        drift = {**lock_ok, "decisions": {**lock_ok["decisions"],
                                          "n2": {"decision_record_id": "older"}}}
        assert coverage.compute_coverage(span_coverage=None, requirements=reqs, needs=needs,
                                         tasks=[], lock=drift)["progress"]["locked"] == 0
        stale = {**lock_ok, "is_usable": False, "stale_reasons": ["x"]}
        assert coverage.compute_coverage(span_coverage=None, requirements=reqs, needs=needs,
                                         tasks=[], lock=stale)["progress"]["locked"] == 0
        undecided = coverage.compute_coverage(
            span_coverage=None, requirements=reqs,
            needs=[cov_need("n1", "ADOPT"), cov_need("n2")], tasks=[], lock=lock_ok)
        assert undecided["progress"]["decided"] == 0

    def test_tasks_and_verification_sources(self):
        def run(latest, task_status="implemented", current=True, checks=1):
            return coverage.compute_coverage(
                span_coverage=None,
                requirements=[cov_req("a", "bespoke", criteria=("x", "y"), latest=latest,
                                      tasks_=["t1"])],
                needs=[],
                tasks=[{"id": "t1", "status": task_status, "is_current": current,
                        "lock_checks": checks}],
                lock={"version": 1, "is_usable": True, "stale_reasons": [], "decisions": {}})

        both = {"a-x": {"outcome": "pass", "recorded_by": "cip_executed"},
                "a-y": {"outcome": "pass", "recorded_by": "cip_executed"}}
        out = run(both)
        assert out["progress"]["verified"] == 1 and out["progress"]["verified_cip_executed"] == 1
        mixed = run({**both, "a-y": {"outcome": "pass", "recorded_by": "agent_reported"}})
        assert mixed["progress"]["verified_agent_reported_only"] == 1
        assert mixed["by_requirement"][0]["verified_by"] == "agent_reported"
        partial = run({"a-x": both["a-x"]})
        assert partial["progress"]["verified"] == 0
        assert partial["by_requirement"][0]["verification_outcome"] == "partial"
        failed = run({**both, "a-y": {"outcome": "fail", "recorded_by": "agent_reported"}})
        assert failed["by_requirement"][0]["verification_outcome"] == "fail"

        assert run(both)["progress"]["implemented"] == 1
        assert run(both, task_status="planned")["progress"]["implemented"] == 0
        stale = run(both, current=False)
        assert stale["progress"]["tasked"] == 0 and stale["compliance"]["tasks_stale"] == 1
        assert run(both, task_status="abandoned")["progress"]["tasked"] == 0
        assert run(both, checks=0)["compliance"]["tasks_worked_without_lock_check"] == 1
        assert run(both, task_status="planned", checks=0)[
            "compliance"]["tasks_worked_without_lock_check"] == 0

    def test_no_criteria_is_never_verified(self):
        out = coverage.compute_coverage(
            span_coverage=None, requirements=[cov_req("a", "bespoke", criteria=())],
            needs=[], tasks=[], lock=None)
        assert out["progress"]["verified"] == 0 and out["progress"]["verified_pct"] == 0


def derive(**over):
    args = dict(
        has_source=True, requirement_count=3, confirmation_blockers=[],
        confirmation_state="approved", needs_total=2, needs_searched=2, needs_decided=2,
        lock_state="none", in_scope=3, verified=0,
    )
    args.update(over)
    return phase.derive_phase(**args)[0]


class TestPhase:

    @pytest.mark.parametrize("over,expect", [
        ({"has_source": False}, "no_pdr"),
        ({"requirement_count": 0}, "requirements_draft"),
        ({"confirmation_state": "none", "confirmation_blockers": ["x"]}, "requirements_draft"),
        ({"confirmation_state": "none"}, "requirements_draft"),
        ({"confirmation_state": "pending"}, "awaiting_requirements_confirmation"),
        ({"confirmation_state": "rejected"}, "requirements_draft"),
        ({"needs_searched": 1, "needs_decided": 0}, "search_and_decide"),
        ({"needs_decided": 1}, "search_and_decide"),
        ({}, "ready_to_lock"),
        ({"needs_total": 0, "needs_searched": 0, "needs_decided": 0}, "ready_to_lock"),
        ({"lock_state": "pending"}, "awaiting_lock_approval"),
        ({"lock_state": "rejected"}, "ready_to_lock"),
        ({"lock_state": "active"}, "building"),
        ({"lock_state": "active", "verified": 3}, "complete"),
        ({"lock_state": "active", "in_scope": 0}, "building"),
        ({"lock_state": "stale"}, "relock_required"),
        # A stale lock does not hide an unconfirmed scope: fix that first.
        ({"lock_state": "stale", "confirmation_state": "none"}, "requirements_draft"),
    ])
    def test_phase(self, over, expect):
        assert derive(**over) == expect
        assert expect in phase.PHASES

    def test_waiting_phases_tell_the_agent_not_to_approve(self):
        for state in ({"confirmation_state": "pending"}, {"lock_state": "pending"}):
            _, actions = phase.derive_phase(**{**dict(
                has_source=True, requirement_count=3, confirmation_blockers=[],
                confirmation_state="approved", needs_total=2, needs_searched=2,
                needs_decided=2, lock_state="none", in_scope=3, verified=0), **state})
            assert any("Do not run that command yourself" in a for a in actions)
