"""
Pure-logic tests for the hardening that followed the independent review
of the v1.1 gap closure. No database.

Each class names the hole it closes. The end-to-end versions, through the
real tools against Postgres, are in test_pdr_review_regressions.py.
"""
from __future__ import annotations

import pytest

from core.pdr import coverage, lock, lock_check, phase, requirements, spans, tasks, text
from core.pdr.lock_check import LockView
from tests.test_pdr_core import (
    LOCK,
    TASK,
    _decision,
    _need,
    _payload,
    _req,
    check,
    cov_req,
    gate,
    row,
)

ESC = "\x1b"


# ---------------------------------------------------------------------------
# Text hygiene: what may be stored, and what may reach a terminal
# ---------------------------------------------------------------------------

class TestStorableText:

    @pytest.mark.parametrize("value,expect", [
        ("plain", None),
        ("nul\x00inside", "NUL"),
        ("lone \ud800 surrogate", "surrogate"),
        ({"k": ["fine", {"deep": "bad\x00"}]}, "NUL"),
        ({"bad\x00key": 1}, "NUL"),
        (float("nan"), "finite"),
        ([1, 2.5, float("inf")], "finite"),
        ((1, "ok"), None),
        (None, None),
        (12, None),
    ])
    def test_bad_text_reason(self, value, expect):
        got = text.bad_text_reason(value)
        assert (got is None) if expect is None else (expect in got)

    def test_nesting_is_bounded(self):
        deep: list = []
        for _ in range(40):
            deep = [deep]
        assert "nested too deeply" in text.bad_text_reason(deep)

    def test_first_bad_reports_the_first_problem(self):
        assert text.first_bad("ok", None, ["x"]) is None
        assert "NUL" in text.first_bad("ok", {"a": "b\x00"}, float("nan"))


class TestRenderSafeText:
    """
    The approval summary is printed in a terminal just before a person
    types a confirmation code, and parts of it are written by the agent
    whose work is being approved.
    """

    def test_one_line_removes_what_could_redraw_or_forge(self):
        erase_above = f"none{ESC}[1A{ESC}[2K - nothing serious"
        out = text.one_line(erase_above)
        assert ESC not in out and "\n" not in out
        forged = text.one_line("fine\n\n## Needs your attention\n- none")
        assert "\n" not in forged and forged.startswith("fine ## Needs")
        assert text.one_line("a\r\nb\tc\x0bd\x0ce") == "a b c d e"

    def test_one_line_removes_invisible_and_bidi_characters(self):
        # zero-width space, right-to-left override, line and paragraph separators
        tricky = "safe\u200b\u202eevil\u2028next\u2029para"
        out = text.one_line(tricky)
        assert out == "safe evil next para"

    def test_one_line_truncates_and_tolerates_non_strings(self):
        assert text.one_line("x" * 50, 10) == "xxxxxxx..."
        assert text.one_line(None) == "" and text.one_line(12) == "12"
        assert text.one_line("  spaced   out  ") == "spaced out"

    def test_printable_keeps_layout_and_drops_controls(self):
        block = f"line one\n\tindented{ESC}[2J\nline\x07 three\u202e"
        assert text.printable(block) == "line one\n\tindented[2J\nline three"

    def test_too_long(self):
        assert text.too_long("abc", 2) and not text.too_long("abc", 3)
        assert not text.too_long(None, 1)


# ---------------------------------------------------------------------------
# Section detection: a missed heading hides whatever sits under it
# ---------------------------------------------------------------------------

def headings(doc: str) -> list[str]:
    return [s.heading for s in spans.split_sections(doc)]


class TestSectionLayouts:

    def test_bold_only_lines_are_headings(self):
        doc = ("**Goals**\nCut missed calls.\n\n**Security:**\nAll data is encrypted.\n\n"
               "__Billing__\nCharge monthly.\n")
        assert headings(doc) == ["Goals", "Security", "Billing"]

    def test_bold_inside_a_sentence_is_not_a_heading(self):
        doc = "# A\nThis is **important** text.\n\n# B\nMore.\n"
        assert headings(doc) == ["A", "B"]

    def test_setext_headings(self):
        doc = "Goals\n=====\nCut missed calls.\n\nSecurity\n--------\nEncrypt it.\n"
        assert headings(doc) == ["Goals", "Security"]

    # A document with no recognised heading falls back to paragraphs, whose
    # "headings" are their first lines. For the plainer styles that gives
    # the same list as real detection would, so the tests above cannot tell
    # the two apart. These put each style under two ordinary headings, where
    # there is no fallback to hide behind: miss the style and its sections
    # are folded into "Details".
    FRAME = "# Overview\nWhat this is.\n\n# Details\nRead on.\n\n"

    @pytest.mark.parametrize("body,expect", [
        ("Security\n--------\nEncrypt it.\n\nBilling\n=======\nCharge monthly.\n",
         ["Security", "Billing"]),
        ("SECURITY AND PRIVACY\nEncrypt it.\n\nBILLING\nCharge monthly.\n",
         ["SECURITY AND PRIVACY", "BILLING"]),
        ("3. Security\nEncrypt it.\n\n3.1 Billing\nCharge monthly.\n",
         ["3. Security", "3.1 Billing"]),
        ("**Security**\nEncrypt it.\n\n__Billing:__\nCharge monthly.\n",
         ["Security", "Billing"]),
    ])
    def test_each_heading_style_is_detected_on_its_own_merits(self, body, expect):
        assert headings(self.FRAME + body) == ["Overview", "Details", *expect]
        cov = spans.span_coverage(self.FRAME + body, [])
        assert [s["heading"] for s in cov["unmapped_sections"]][-2:] == expect

    def test_fenced_code_is_not_scanned_for_headings(self):
        doc = self.FRAME + "```\nSECURITY\n\n3. Billing\n\n**Bold**\n```\nafter\n"
        assert headings(doc) == ["Overview", "Details"]

    def test_a_list_item_above_a_rule_is_not_a_setext_heading(self):
        doc = "# A\n- item one\n---\nafter the rule\n\n# B\nbody\n"
        assert headings(doc) == ["A", "B"]

    def test_numbered_titles(self):
        doc = ("1. Goals\nCut missed calls.\n\n2. Requirements\nStaff must sign in.\n\n"
               "2.1 Security\nEncrypt it.\n")
        assert headings(doc) == ["1. Goals", "2. Requirements", "2.1 Security"]

    def test_a_numbered_sentence_is_not_a_heading(self):
        doc = ("# Requirements\n\n1. Staff must sign in with Google before using it.\n\n"
               "2. The receptionist sees today's bookings.\n\n# Other\nx\n")
        assert headings(doc) == ["Requirements", "Other"]

    def test_all_caps_lines(self):
        doc = "GOALS\nCut missed calls.\n\nSECURITY AND PRIVACY\nEncrypt it.\n"
        assert headings(doc) == ["GOALS", "SECURITY AND PRIVACY"]

    def test_an_unclosed_fence_does_not_swallow_the_document(self):
        """One stray ``` used to hide every heading after it."""
        doc = "# T\nintro\n```\ncode never closed\n# A\nalpha\n# B\nbeta\n# C\ngamma\n"
        assert headings(doc) == ["T", "A", "B", "C"]

    def test_a_closed_fence_still_hides_its_contents(self):
        doc = "# A\n```\n# not a heading\n```\nbody\n# B\nmore\n"
        assert headings(doc) == ["A", "B"]

    def test_no_headings_falls_back_to_paragraphs(self):
        doc = ("The system books appointments.\n\nStaff must sign in first.\n\n"
               "Payments are taken by card.\n")
        secs = spans.split_sections(doc)
        assert len(secs) == 3 and all(s.has_body for s in secs)
        cov = spans.span_coverage(doc, [spans.locate_quote(doc, "Staff must sign in first.")])
        assert cov["section_count"] == 3 and cov["mapped_section_count"] == 1

    def test_sections_tile_the_document(self):
        doc = "Intro words.\n\n**A**\nalpha\n\nB\n=\nbeta\n\n3. Gamma\ngamma text\n"
        secs = spans.split_sections(doc)
        assert secs[0].heading == "(preamble)" and secs[0].start == 0
        for a, b in zip(secs, secs[1:]):
            assert a.end == b.start
        assert secs[-1].end == len(doc)

    def test_a_rule_only_section_has_no_body(self):
        doc = "# A\n---\n\n# B\nreal words\n\n# C\nmore words\n"
        by = {s.heading: s.has_body for s in spans.split_sections(doc)}
        assert by == {"A": False, "B": True, "C": True}


# ---------------------------------------------------------------------------
# The confirmed scope includes what will be searched for
# ---------------------------------------------------------------------------

class TestSearchTermsAreConfirmedContent:

    def _hash(self, needs):
        return requirements.requirements_hash(requirements.requirements_snapshot(
            source_content_hash="h", requirements=[_req("a")], needs=needs))

    def test_search_terms_move_the_hash(self):
        assert self._hash([_need("n", ["a"], search_terms=["oauth"])]) != \
            self._hash([_need("n", ["a"], search_terms=["zzz-nothing"])])

    def test_problem_types_move_the_hash(self):
        assert self._hash([_need("n", ["a"], problem_types=["oauth-flow"])]) != \
            self._hash([_need("n", ["a"], problem_types=[])])

    def test_term_order_does_not(self):
        assert self._hash([_need("n", ["a"], search_terms=["b", "a"])]) == \
            self._hash([_need("n", ["a"], search_terms=["a", "b"])])


class TestRequirementDraftHardening:

    BASE = {"text": "Staff sign in", "quote": "Staff must sign in with Google OAuth.",
            "acceptance_criteria": ["works"]}
    DOC = "Intro.\nStaff must sign in with Google OAuth.\nOutro.\n"

    def test_slug_explicit_is_reported(self):
        derived, _ = requirements.validate_requirement_draft(self.BASE, self.DOC)
        named, _ = requirements.validate_requirement_draft({**self.BASE, "slug": "sign-in"}, self.DOC)
        assert derived["slug_explicit"] is False and named["slug_explicit"] is True

    def test_field_and_criteria_limits(self):
        long_text = {**self.BASE, "text": "x" * (requirements.MAX_FIELD_CHARS + 1)}
        many = {**self.BASE,
                "acceptance_criteria": [f"c{i}" for i in range(requirements.MAX_CRITERIA + 1)]}
        assert requirements.validate_requirement_draft(long_text, self.DOC)[1]
        assert requirements.validate_requirement_draft(many, self.DOC)[1]

    @pytest.mark.parametrize("field,value", [
        ("text", 5), ("quote", 5), ("acceptance_criteria", [{"a": 1}]),
        ("priority", ["could"]), ("kind", 7), ("disposition", {"x": 1}), ("slug", 12),
    ])
    def test_wrong_types_are_errors_not_exceptions_or_defaults(self, field, value):
        clean, errors = requirements.validate_requirement_draft({**self.BASE, field: value}, self.DOC)
        assert errors, clean

    @pytest.mark.parametrize("raw", [None, 5, "x", []])
    def test_a_non_object_is_an_error(self, raw):
        assert requirements.validate_requirement_draft(raw, self.DOC)[1]
        assert requirements.validate_need_draft(raw)[1]


# ---------------------------------------------------------------------------
# Lock payload: approvals are tied to history; the summary is the payload
# ---------------------------------------------------------------------------

class TestLockPayloadHistory:

    def test_replaces_version_changes_the_approval_hash_not_the_content(self):
        """
        The same content approved to replace v1 must not be replayable to
        replace v2: the hash the human approves includes what it replaces.
        Whether a relock changed anything is judged without it.
        """
        first = _payload()
        after_v1 = _payload(replaces_lock_version=1)
        after_v2 = _payload(replaces_lock_version=2)
        assert len({lock.payload_hash(p) for p in (first, after_v1, after_v2)}) == 3
        assert len({lock.content_core_hash(p) for p in (first, after_v1, after_v2)}) == 1
        assert first["replaces_lock_version"] is None

    def test_content_core_hash_moves_with_content(self):
        assert lock.content_core_hash(_payload()) != lock.content_core_hash(
            _payload(unresolved_risks=["new risk"]))

    def test_decisions_carry_their_search_terms_into_the_cold_start_flag(self):
        cold = _decision(verdict="BUILD", coverage_outcome="no_registry_coverage",
                         flags=["cold_start_build"], search_terms=["oauth", "sso"])
        flags = lock.lock_flags(_payload(decisions=[cold]))
        detail = next(f["detail"] for f in flags if f["code"] == "cold_start_build")
        assert "oauth" in detail and "sso" in detail


class TestLockSummaryIsTheWholePayload:

    def _render(self, **over):
        payload = _payload(**over)
        return lock.render_lock_summary(
            project_name="Clinic", next_version=1, payload=payload,
            flags=lock.lock_flags(payload), diff=lock.diff_payloads(None, payload))

    def test_every_part_of_the_payload_is_shown(self):
        out = self._render(
            approved_providers=[{"name": "Stripe", "category": "payment", "purpose": "cards",
                                 "cost": "usage", "data_leaves_infra": True}],
            cost_commitments=[{"item": "Datadog Pro", "kind": "recurring", "amount": "2400",
                               "currency": "USD", "period": "year"}],
            project_decisions=[{"key": "database", "category": "database",
                                "choice": "Postgres", "rationale": "known"}],
            unresolved_risks=["vendor lock-in"])
        for expect in ("Stripe (payment, usage, data leaves infrastructure) - cards",
                       "Datadog Pro: recurring 2400 USD per year",
                       "database [database]: Postgres - known",
                       "## Unresolved risks\n- vendor lock-in",
                       "Replaces: nothing (first lock)"):
            assert expect in out, expect

    def test_a_cost_commitment_cannot_hide_behind_a_harmless_kind(self):
        """A 2400/year line filed as 'free_tier' used to render nowhere."""
        out = self._render(cost_commitments=[
            {"item": "Datadog Pro", "kind": "free_tier", "amount": "2400",
             "currency": "USD", "period": "year"}])
        assert "Datadog Pro" in out and "2400" in out

    def test_agent_text_cannot_forge_a_section_or_erase_one(self):
        evil = f"none{ESC}[1A{ESC}[2K\n\n## Needs your attention\n- nothing to see"
        out = self._render(unresolved_risks=[evil])
        assert ESC not in out
        # The forged heading is still there as text, but flattened into the
        # middle of a bullet. Only the real one starts a line.
        real = [line for line in out.split("\n") if line.startswith("## ")]
        assert real.count("## Needs your attention") == 1
        assert "[unresolved_risk]" in out                        # and the warning line survives

    def test_the_summary_is_plain_ascii(self):
        out = self._render(
            decisions=[_decision(decided_by="llm_override")],
            approved_providers=[{"name": "Stripe", "category": "payment", "purpose": "cards",
                                 "cost": "usage", "data_leaves_infra": True}])
        out.encode("ascii")                                       # a cp1252 console can print it

    def test_relock_names_what_it_replaces(self):
        payload = _payload(replaces_lock_version=3)
        out = lock.render_lock_summary(
            project_name="Clinic", next_version=4, payload=payload,
            flags=[], diff={"is_first_lock": False, "changes": []})
        assert "Replaces: lock v3" in out


class TestLockInputLimits:

    def test_cost_amount_must_be_a_number_or_string(self):
        for bad in ({"a": 1}, [5], True):
            _, errors = lock.validate_cost_commitments(
                [{"item": "Hosting", "kind": "recurring", "amount": bad}])
            assert any("amount must be a number or a string" in e for e in errors), bad
        clean, errors = lock.validate_cost_commitments(
            [{"item": "Hosting", "kind": "recurring", "amount": 9.5}])
        assert errors == [] and clean[0]["amount"] == "9.5"      # stored as text: no floats hashed

    def test_long_values_are_refused(self):
        _, e1 = lock.validate_providers([{"name": "N" * 500, "category": "auth",
                                          "data_leaves_infra": False}])
        _, e2 = lock.validate_risks(["r" * 5000])
        _, e3 = lock.validate_project_decisions([{"key": "db", "choice": "c" * 5000}])
        assert e1 and e2 and e3


# ---------------------------------------------------------------------------
# Lock check: anything not understood was not checked
# ---------------------------------------------------------------------------

class TestLockCheckFailsClosed:

    def test_unknown_top_level_key_escalates(self):
        out = check({"provider_changes": [{"name": "Google", "action": "use"}],
                     "new_services": ["OpenAI"]})
        assert out.verdict == lock_check.NEEDS_APPROVAL
        assert "Unrecognised key 'new_services'" in out.reason

    def test_unknown_item_key_escalates(self):
        out = check({"capability_changes": [{"capability": "auth", "action": "swap"}]})
        assert out.verdict == lock_check.NEEDS_APPROVAL and "unrecognised key(s)" in out.reason

    def test_using_an_unapproved_provider_escalates(self):
        """action 'use' on a provider that is not in the lock used to pass."""
        assert check({"provider_changes": [{"name": "Google", "action": "use"}]}).verdict == \
            lock_check.ALLOWED
        out = check({"provider_changes": [{"name": "OpenAI", "action": "use"}]})
        assert out.verdict == lock_check.NEEDS_APPROVAL and "OpenAI" in out.reason

    def test_an_unknown_provider_action_escalates(self):
        out = check({"provider_changes": [{"name": "Google", "action": "introduce"}]})
        assert out.verdict == lock_check.NEEDS_APPROVAL and "unknown action" in out.reason

    @pytest.mark.parametrize("paths", [[["src/auth/x.py"]], [5], [None, "src/auth/a.py"]])
    def test_non_string_paths_escalate(self, paths):
        out = check({"build_task_id": "t1", "changed_paths": paths})
        assert out.verdict == lock_check.NEEDS_APPROVAL
        assert "must be a list of path strings" in out.reason

    def test_a_capability_change_must_say_what_it_changes_to(self):
        out = check({"capability_changes": [{"need": "auth"}]})
        assert out.verdict == lock_check.NEEDS_APPROVAL and "must state what the need would use" in out.reason
        same = check({"capability_changes": [{"need": "auth", "capability_version_id": "cv1"}]})
        assert same.verdict == lock_check.ALLOWED
        to_none = check({"capability_changes": [{"need": "auth", "capability_version_id": None}]})
        assert to_none.verdict == lock_check.NEEDS_APPROVAL

    def test_malformed_entries_escalate(self):
        for change in ({"dependency_adds": ["requests"]},
                       {"provider_changes": [{"action": "add"}]},
                       {"capability_changes": [{"need": 5, "verdict": "ADOPT"}]},
                       {"dependency_adds": [{"name": "requests"}]}):
            assert check(change).verdict == lock_check.NEEDS_APPROVAL, change

    def test_echoed_input_is_bounded_and_single_line(self):
        out = check({"provider_changes": [{"name": "x" * 5000 + f"\n{ESC}[2J", "action": "add"}]})
        assert len(out.reason) < 400 and ESC not in out.reason and "\n" not in out.reason


class TestLockCheckProjectDecisions:
    """Cross-cutting decisions were stored in the lock and never checked."""

    WITH_DB = LockView(
        version=LOCK.version, stale_reasons=(), decisions=LOCK.decisions,
        approved_providers=LOCK.approved_providers,
        project_decisions=({"key": "database", "category": "database", "choice": "Postgres"},),
    )

    def test_same_choice_is_allowed(self):
        out = check({"project_decision_changes": [{"key": "Database", "choice": "postgres"}]},
                    lock_view=self.WITH_DB)
        assert out.verdict == lock_check.ALLOWED

    def test_a_different_choice_needs_approval_and_cites_the_lock(self):
        out = check({"project_decision_changes": [{"key": "database", "choice": "MongoDB"}]},
                    lock_view=self.WITH_DB)
        assert out.verdict == lock_check.NEEDS_APPROVAL
        assert "is 'Postgres'" in out.reason and out.cited[0]["type"] == "project_decision"

    def test_a_decision_the_lock_does_not_hold_needs_approval(self):
        out = check({"project_decision_changes": [{"key": "hosting", "choice": "Fly"}]},
                    lock_view=self.WITH_DB)
        assert out.verdict == lock_check.NEEDS_APPROVAL and "not a locked project decision" in out.reason

    def test_it_counts_as_checkable_content(self):
        out = check({"description": "switch db",
                     "project_decision_changes": [{"key": "database", "choice": "Postgres"}]},
                    lock_view=self.WITH_DB)
        assert out.verdict == lock_check.ALLOWED


class TestPermittedPathPatterns:

    @pytest.mark.parametrize("pattern", ["**", "*", "**/x.py", "*/src", "?rc/", "[a-z]*/x"])
    def test_a_leading_wildcard_is_refused(self, pattern):
        """'**' permitted every file in the repository."""
        assert "leading wildcard" in lock_check.path_pattern_problem(pattern)
        assert gate([row("screen", disposition="bespoke")], permitted_paths=[pattern])

    @pytest.mark.parametrize("pattern", ["src/", "src/**", "src/auth/*.py", "tests/test_auth*.py",
                                         "README.md", "./src/app"])
    def test_literal_first_segment_is_fine(self, pattern):
        assert lock_check.path_pattern_problem(pattern) is None

    @pytest.mark.parametrize("pattern,expect", [
        ("", "non-empty"), ("   ", "non-empty"), (None, "non-empty"), (5, "non-empty"),
        (".", "repo-relative"), ("./", "repo-relative"), ("../x", "repo-relative"),
        ("/abs", "repo-relative"), ("src/../..", "repo-relative"), ("C:\\x", "repo-relative"),
    ])
    def test_other_bad_patterns(self, pattern, expect):
        assert expect in lock_check.path_pattern_problem(pattern)

    def test_gate_refuses_a_non_list_and_too_many(self):
        rows = [row("screen", disposition="bespoke")]
        assert "must be a list" in gate(rows, permitted_paths="src/")[0]
        assert any("at most" in r for r in
                   gate(rows, permitted_paths=[f"src/{i}" for i in range(tasks.MAX_PATHS + 1)]))


# ---------------------------------------------------------------------------
# Verification evidence
# ---------------------------------------------------------------------------

GOOD = {"outcome": "pass", "criterion_id": "c1", "commit_sha": "abc1234def",
        "test_ref": "t::x", "command": "pytest", "exit_code": 0}


class TestVerificationEvidence:

    @pytest.mark.parametrize("sha", ["unknown", "HEAD", "main", "abc12", "g" * 40, "abc1234 def"])
    def test_commit_sha_must_look_like_a_commit(self, sha):
        clean, errors = tasks.validate_verification({**GOOD, "commit_sha": sha})
        assert clean is None and any("git commit hash" in e for e in errors)

    @pytest.mark.parametrize("sha", ["abc1234", "A" * 40, "0" * 64])
    def test_real_shapes_are_accepted(self, sha):
        assert tasks.validate_verification({**GOOD, "commit_sha": sha})[1] == []

    def test_a_windows_crash_code_is_recordable_as_a_fail(self):
        """0xC0000005 does not fit a 32-bit signed column; the fail must still be stored."""
        clean, errors = tasks.validate_verification(
            {"outcome": "fail", "criterion_id": "c1", "exit_code": 3221225477})
        assert errors == [] and clean["exit_code"] == 3221225477

    def test_exit_code_types_and_range(self):
        assert tasks.validate_verification({**GOOD, "exit_code": "0"})[1]       # a string is not 0
        assert tasks.validate_verification({**GOOD, "exit_code": False})[1]     # nor is False
        assert any("out of range" in e for e in
                   tasks.validate_verification({**GOOD, "outcome": "fail", "exit_code": 2 ** 70})[1])

    def test_long_fields_are_refused(self):
        assert tasks.validate_verification({**GOOD, "command": "x" * 3000})[1]
        assert tasks.validate_verification({**GOOD, "test_ref": "x" * 600})[1]

    def test_worktree_dirty_is_kept_only_as_a_boolean(self):
        assert tasks.validate_verification({**GOOD, "worktree_dirty": True})[0]["worktree_dirty"] is True
        assert tasks.validate_verification({**GOOD, "worktree_dirty": "no"})[0]["worktree_dirty"] is None

    def test_text_lists(self):
        assert tasks.validate_text_list(None, "x") == ([], None)
        assert tasks.validate_text_list([" a ", "", "b"], "x") == (["a", "b"], None)
        # A bare string used to be stored as a list of its characters.
        assert "must be a list" in tasks.validate_text_list("do not touch auth", "x")[1]
        assert "only strings" in tasks.validate_text_list(["a", 5], "x")[1]
        assert tasks.validate_text_list(["a"] * 60, "x")[1]
        assert tasks.validate_text_list(["a" * 400], "x")[1]


class TestCoverageEvidenceHonesty:

    def _run(self, latest):
        return coverage.compute_coverage(
            span_coverage=None,
            requirements=[cov_req("a", "bespoke", latest=latest, tasks_=["t1"])],
            needs=[],
            tasks=[{"id": "t1", "status": "verified", "is_current": True, "lock_checks": 1}],
            lock={"version": 1, "is_usable": True, "stale_reasons": [], "decisions": {}})

    def test_a_pass_on_a_dirty_tree_is_not_cip_executed(self):
        clean = self._run({"a-c": {"outcome": "pass", "recorded_by": "cip_executed",
                                   "worktree_dirty": False}})
        dirty = self._run({"a-c": {"outcome": "pass", "recorded_by": "cip_executed",
                                   "worktree_dirty": True}})
        assert clean["progress"]["verified_cip_executed"] == 1
        assert dirty["progress"]["verified"] == 1
        assert dirty["progress"]["verified_cip_executed"] == 0
        assert dirty["by_requirement"][0]["verified_by"] == "agent_reported"

    def test_the_command_is_returned_so_a_person_can_read_it(self):
        out = self._run({"a-c": {"outcome": "pass", "recorded_by": "cip_executed",
                                 "worktree_dirty": False, "command": "python -c pass",
                                 "test_ref": "noop", "commit_sha": "abc1234"}})
        ev = out["by_requirement"][0]["evidence"][0]
        assert ev["command"] == "python -c pass" and ev["commit_sha"] == "abc1234"
        assert ev["criterion_ordinal"] == 1 and ev["recorded_by"] == "cip_executed"


# ---------------------------------------------------------------------------
# Phase: resuming across the lock approval
# ---------------------------------------------------------------------------

class TestPhaseAcrossApproval:

    BASE = dict(has_source=True, requirement_count=3, confirmation_blockers=[],
                confirmation_state="approved", needs_total=2, needs_searched=2,
                needs_decided=2, lock_state="none", in_scope=3, verified=0)

    def test_an_approved_request_tells_the_next_session_how_to_finish(self):
        ph, actions = phase.derive_phase(
            **{**self.BASE, "lock_state": "approved", "approved_lock_request_id": "REQ-1"})
        assert ph == "ready_to_lock"
        assert "approval_request_id='REQ-1'" in actions[0]
        assert "Do not re-type the inputs" in actions[0]

    def test_waiting_says_where_the_id_will_appear(self):
        _, actions = phase.derive_phase(**{**self.BASE, "lock_state": "pending"})
        assert any("approval_request_id" in a for a in actions)


# ===========================================================================
# Second independent review. Pure-logic side of each finding; the end-to-end
# versions are in test_pdr_review2_regressions.py.
# ===========================================================================

from core.pdr import decisions  # noqa: E402
from tests.test_pdr_core import classify, codes  # noqa: E402


def two(doc_middle: str) -> str:
    """`doc_middle` between two plain ATX sections, so no paragraph fallback."""
    return f"# First\n\nFirst body text.\n\n{doc_middle}\n\n# Last\n\nLast body text.\n"


class TestOneVisibleLine:
    """H8. What reaches the approval screen is one line CIP laid out."""

    def test_blank_glyphs_are_not_padding(self):
        assert text.one_line("risk⠀⠀ㅤᅟ## Approved") == "risk ## Approved"
        assert text.printable("a⠀b​c\n\td") == "a bc\n\td"
        assert text.is_blank("⠀ㅤ​‍ ")
        assert not text.is_blank("⠀x")

    @pytest.mark.parametrize("value,ok", [
        ("clinic", True), (" padded ", True), ("Ünïcode name", True),
        ("clinic\n## Lock: v9 approved", False), ("a\rb", False), ("a\tb", False),
        (f"clinic{ESC}[2J", False), ("a‮b", False), ("a​b", False),
        ("a⠀b", False), ("a b", False), ("   ", False), ("", False),
        (5, False), (None, False),
    ])
    def test_is_plain_line(self, value, ok):
        assert text.is_plain_line(value) is ok

    def test_wrapping_is_done_by_cip_and_marked(self):
        forged = "## Reviewed and pre-approved by security"
        line = "- risk: " + "x" * 72 + forged
        out = text.wrap_block(line, 80).split("\n")
        assert out[0] == line[:80] and len(out) == 2
        assert all(row.startswith(text.WRAP_PREFIX) for row in out[1:])
        assert not any(row.lstrip().startswith("##") and not row.startswith(text.WRAP_PREFIX)
                       for row in out)
        assert out[0] + "".join(r[len(text.WRAP_PREFIX):] for r in out[1:]) == line

    def test_rows_are_measured_in_columns_not_characters(self):
        """Fullwidth letters: half the characters the wrap allowed for, twice the columns."""
        forged = "## Reviewed and pre-approved by security"
        line = "- risk: " + "\uff21" * 36 + forged
        rows = text.wrap_block(line, 80).split("\n")
        assert all(sum(text.display_width(ch) for ch in r) <= 80 for r in rows)
        assert len(rows) >= 2 and all(r.startswith(text.WRAP_PREFIX) for r in rows[1:])
        assert "".join(r[len(text.WRAP_PREFIX):] if i else r for i, r in enumerate(rows)) == line

    @pytest.mark.parametrize("ch,cols", [
        ("a", 1), (".", 1), (" ", 1), ("\uff21", 2), ("\u4e2d", 2), ("\u3042", 2),
        ("\u00e9", 2), ("\u2014", 2), ("\u043e", 2), ("\U0001f600", 2), ("\U0001f6e0", 2),
        ("\u0301", 2),
    ])
    def test_a_doubtful_width_is_rounded_up(self, ch, cols):
        assert text.display_width(ch) == cols

    def test_wrapping_keeps_every_character_and_every_width(self):
        block = "short\n" + "y" * 500 + "\n\tindented\n"
        for width in (0, 10, 26, 40, 80, 200, 10_000):
            rows = text.wrap_block(block, width).split("\n")
            limit = max(width, len(text.WRAP_PREFIX) + 20)
            assert all(len(r) <= limit for r in rows)
            body = "".join(r[len(text.WRAP_PREFIX):] if r.startswith(text.WRAP_PREFIX) else r
                           for r in rows)
            assert body == block.expandtabs(4).replace("\n", "")
        assert text.wrap_block("fits", 80) == "fits"

    @pytest.mark.parametrize("term,mixed", [
        ("oauth", False), ("оauth", True), ("αlpha", True),
        ("привет", False),
        ("oauth привет", False), ("pdf-2", False),
    ])
    def test_lookalike_letters_inside_a_word(self, term, mixed):
        assert text.mixed_script(term) is mixed

    def test_one_name_has_one_spelling(self):
        assert text.nfc("Å") == "Å"
        a, _ = requirements.validate_need_draft(
            {"description": "café", "slug": "café", "search_terms": ["cafe"],
             "satisfies": ["r"]})[:2]
        b, _ = requirements.validate_need_draft(
            {"description": "café", "slug": "café", "search_terms": ["cafe"],
             "satisfies": ["r"]})[:2]
        assert a["description"] == b["description"] and a["slug"] == b["slug"]


class TestFencesByTheBook:
    """M5. A fence read wrongly hides every heading after it."""

    def test_a_longer_fence_can_show_a_shorter_one(self):
        doc = two("````md\n```\n# not a heading\n```\n````\n\n# Real\n\nReal body.")
        assert headings(doc) == ["First", "Real", "Last"]

    def test_a_stray_fence_does_not_turn_later_blocks_inside_out(self):
        doc = two("```\n\n# Hidden no more\n\nIts body.\n\n```json\n{\"a\": 1}\n```\n\n"
                  "# After\n\nAfter body.")
        assert headings(doc) == ["First", "Hidden no more", "After", "Last"]

    def test_an_unclosed_fence_hides_nothing(self):
        doc = "# A\n\nBody a.\n\n```\n# B\n\nBody b.\n\n# C\n\nBody c.\n"
        assert headings(doc) == ["A", "B", "C"]

    def test_closed_fences_still_hide_their_comments(self):
        doc = two("```sh\n# install\npip install x\n```\n\n~~~python\n# also code\n~~~")
        assert headings(doc) == ["First", "Last"]

    def test_a_bare_row_of_tildes_is_a_divider_not_a_fence(self):
        """A pair of ~~~~ dividers hid every heading between them."""
        doc = "# A\n\nBody a.\n\n~~~~~~~~\n\n# B\n\nBody b.\n\n~~~~~~~~\n\n# C\n\nBody c.\n"
        assert headings(doc) == ["A", "B", "C"]

    def test_a_fence_with_text_after_it_opens_and_never_closes(self):
        # "``` end" cannot close a block, so it is read as an opener and the
        # fence above it as a stray. The cost is the stated one: a comment
        # shows up as a heading. The real section after the block is found.
        doc = two("```\n# code\n``` end\n# still code\n```\n\n# Real\n\nBody.")
        assert headings(doc) == ["First", "code", "Real", "Last"]

    def test_inline_code_on_one_line_is_not_a_fence(self):
        doc = two("Use ```x``` here.\n\n``` not ` a fence\n\n# Real\n\nBody.")
        assert "Real" in headings(doc)

    def test_a_tilde_row_under_a_title_is_an_underline(self):
        doc = "Goals\n~~~~~\n\nCut missed calls.\n\nSecurity\n~~~~~~~~\n\nEncrypt it.\n"
        assert headings(doc) == ["Goals", "Security"]


class TestMoreHeadingLayouts:
    """M5. Layouts the first pass did not read as headings."""

    @pytest.mark.parametrize("middle,expect", [
        ("   ## Indented three\n\nBody.", "Indented three"),
        ("## Closed ##\n\nBody.", "Closed"),
        ("# C#\n\nBody.", "C#"),
        ("<h2>Security</h2>\n\nBody.", "Security"),
        ("<H3 class=\"x\"><b>Billing</b></H3>\n\nBody.", "Billing"),
        ("== Data retention\n\nBody.", "Data retention"),
        ("Scope\n^^^^^\n\nBody.", "Scope"),
        ("Scope\n*****\n\nBody.", "Scope"),
        ("Scope\n+++++\n\nBody.", "Scope"),
        ("SECURITY REQUIREMENTS\n\nBody text, after a blank line.", "SECURITY REQUIREMENTS"),
        ("A. Compliance\n\nBody text for compliance.", "A. Compliance"),
        ("B) Reporting duties\n\nBody text for reporting.", "B) Reporting duties"),
        ("Section 2: Security\nAll data must be encrypted at rest.", "Section 2: Security"),
        ("Billing (phase 2)\nInvoices are sent monthly by email.", "Billing (phase 2)"),
    ])
    def test_recognised(self, middle, expect):
        assert headings(two(middle)) == ["First", expect, "Last"]

    @pytest.mark.parametrize("middle", [
        "    # four spaces is code\n\nBody.",
        "A sentence that ends properly.\nAnother line of the same paragraph.",
        "- A list item\n- Another one that is rather longer than it",
        "| Name | Value |\n| ---- | ----- |",
        "> Quoted line\nfollowed by a longer line of ordinary text.",
        "the lower-case start\nis followed by a longer line of ordinary text.",
        "A paragraph line that is long\n***\n",
        "Short\nTiny",
    ])
    def test_not_recognised(self, middle):
        assert headings(two(middle)) == ["First", "Last"]

    def test_a_long_line_of_hashes_costs_nothing(self):
        import time
        doc = two("#" * 16_000 + " x\n\n" + ("# " + "#" * 180 + "\n") * 200)
        started = time.perf_counter()
        spans.split_sections(doc)
        assert time.perf_counter() - started < 2.0


class TestAQuoteIsOneSentenceInOnePlace:
    """H3. One quote must not be able to map the whole document."""

    DOC = ("# Goals\n\nCut missed calls by half.\n\n# Security\n\n"
           "All data is encrypted at rest.\n\n# Billing\n\nCharge monthly.\n")

    def draft(self, quote, **over):
        return requirements.validate_requirement_draft(
            {"text": "t", "quote": quote, "acceptance_criteria": ["c"], **over}, self.DOC)

    def test_a_quote_across_a_heading_is_refused(self):
        loc = spans.QuoteLocator(self.DOC)
        inside = loc.locate("All data is encrypted at rest.")
        assert loc.heading_inside(inside) is None
        across = loc.locate("Cut missed calls by half.\n\n# Security\n\nAll data is encrypted")
        assert loc.heading_inside(across) == "Security"
        clean, errors = self.draft("Cut missed calls by half. # Security All data is encrypted")
        assert clean is None and "runs across the heading 'Security'" in errors[0]

    def test_a_quote_may_start_at_a_heading_but_not_pass_the_next(self):
        loc = spans.QuoteLocator(self.DOC)
        own = loc.locate("# Security\n\nAll data is encrypted at rest.")
        assert loc.heading_inside(own) is None
        clean, errors = self.draft(self.DOC.strip())
        assert clean is None and "runs across the heading" in errors[0]

    def test_a_quote_has_a_ceiling(self):
        long_doc = "# A\n\n" + ("word " * 200) + "\n\n# B\n\nBody.\n"
        quote = ("word " * 200).strip()
        assert len(quote) > spans.MAX_QUOTE_CHARS
        clean, errors = requirements.validate_requirement_draft(
            {"text": "t", "quote": quote, "acceptance_criteria": ["c"]}, long_doc)
        assert clean is None and f"longer than {spans.MAX_QUOTE_CHARS}" in errors[0]
        ok, errors = requirements.validate_requirement_draft(
            {"text": "t", "quote": quote[:spans.MAX_QUOTE_CHARS].strip(),
             "acceptance_criteria": ["c"]}, long_doc)
        assert errors == [] and ok["span"]["end"] - ok["span"]["start"] <= spans.MAX_QUOTE_CHARS

    def test_the_locator_does_its_expensive_work_once(self):
        loc = spans.QuoteLocator(self.DOC)
        assert loc.locate("Cut  missed\n calls by half.") is not None
        first = loc._collapsed
        assert loc.locate("All data is   encrypted at rest.") is not None
        assert loc._collapsed is first and loc.sections is loc.sections
        assert spans.locate_quote(self.DOC, "Charge monthly.") == loc.locate("Charge monthly.")

    def test_out_of_scope_quotes_do_not_make_a_section_read(self):
        loc = spans.QuoteLocator(self.DOC)
        goals, security = loc.locate("Cut missed calls by half."), loc.locate(
            "All data is encrypted at rest.")
        cov = spans.span_coverage(self.DOC, [goals], out_of_scope_spans=[security])
        assert [s["heading"] for s in cov["out_of_scope_only_sections"]] == ["Security"]
        assert [s["heading"] for s in cov["unmapped_sections"]] == ["Billing"]
        assert cov["mapped_section_count"] == 1 and cov["section_count"] == 3
        both = spans.span_coverage(self.DOC, [goals, security], out_of_scope_spans=[security])
        assert both["out_of_scope_only_sections"] == []

    def test_a_long_section_mapped_by_one_row_is_listed(self):
        rows = "\n".join(f"- Requirement number {i} must hold." for i in range(30))
        doc = f"# Table\n\n{rows}\n\n# Small\n\nOne short line here.\n"
        loc = spans.QuoteLocator(doc)
        cov = spans.span_coverage(
            doc, [loc.locate("Requirement number 3 must hold."),
                  loc.locate("One short line here.")], sections=loc.sections)
        light = cov["lightly_quoted_sections"]
        assert [s["heading"] for s in light] == ["Table"]
        assert 0 < light[0]["quoted_pct"] < spans.LIGHT_SECTION_MAX_PCT
        assert cov["unmapped_sections"] == []

    def test_overlapping_quotes_never_exceed_the_whole(self):
        doc = "# A\n\nAll data is encrypted at rest.\n"
        s = spans.QuoteLocator(doc).locate("All data is encrypted at rest.")
        cov = spans.span_coverage(doc, [s, s, spans.Span(0, len(doc))],
                                  out_of_scope_spans=[s, spans.Span(-5, 3), spans.Span(4, 10**6)])
        assert cov["quoted_char_pct"] == 100
        assert all(r["quoted_pct"] <= 100 for r in cov["sections"])


class TestWhatIsConfirmedIsWhatIsSearched:
    """M2, H5. The fingerprint, the snapshot and the diff between two of them."""

    def test_problem_types_are_part_of_what_a_search_depends_on(self):
        base = dict(search_terms=["OAuth", "sso"], constraints=[])
        fp = requirements.need_fingerprint
        assert fp(**base) == fp(search_terms=["sso", "oauth"], constraints=[], problem_types=[])
        assert fp(**base, problem_types=["oauth-flow"]) != fp(**base)
        assert fp(**base, problem_types=["a", "b"]) == fp(**base, problem_types=["b", "a"])

    def test_moving_the_anchor_changes_the_confirmed_scope(self):
        def snap(start):
            return requirements.requirements_snapshot(
                source_content_hash="h", needs=[],
                requirements=[{**_req("a", "bespoke"), "span": {"start": start, "end": start + 9}}])
        assert requirements.requirements_hash(snap(10)) != requirements.requirements_hash(snap(50))
        assert snap(10)["requirements"][0]["span"] == [10, 19]
        changes = requirements.diff_snapshots(snap(10), snap(50))
        assert [c["kind"] for c in changes] == ["requirement_changed"]
        assert "anchored at a different place" in changes[0]["detail"]

    def snapshot(self, reqs, needs, source="h"):
        return requirements.requirements_snapshot(
            source_content_hash=source, requirements=reqs, needs=needs)

    def test_nothing_to_compare_and_nothing_changed(self):
        s = self.snapshot([_req("a")], [_need("n", ["a"])])
        assert requirements.diff_snapshots(None, s) == []
        assert requirements.diff_snapshots({}, s) == []
        assert requirements.diff_snapshots(s, s) == []

    def test_every_kind_of_change_is_itemised(self):
        old = self.snapshot(
            [_req("keep", priority="must", acceptance_criteria=["one", "two"]),
             _req("gone", priority="must", text="Export to PDF")],
            [_need("n", ["keep"], search_terms=["oauth"], problem_types=["oauth-flow"],
                   constraints=[{"kind": "license_allowlist", "detail": {"spdx_ids": ["MIT"]}}]),
             _need("dropped", ["gone"])])
        new = self.snapshot(
            [_req("keep", priority="could", acceptance_criteria=["one"],
                  disposition="deferred", disposition_reason="later"),
             _req("fresh", text="Send reminders")],
            [_need("n", ["keep", "fresh"], search_terms=["zzz"], problem_types=[], constraints=[]),
             _need("added", ["fresh"])],
            source="h2")
        by_kind: dict[str, list[dict]] = {}
        for c in requirements.diff_snapshots(old, new):
            by_kind.setdefault(c["kind"], []).append(c)
        assert set(by_kind) == {"pdr_text_changed", "requirement_removed", "requirement_added",
                                "requirement_changed", "need_removed", "need_added", "need_changed"}
        assert by_kind["requirement_removed"][0]["subject"] == "gone"
        assert "[must] Export to PDF" in by_kind["requirement_removed"][0]["detail"]
        changed = by_kind["requirement_changed"][0]["detail"]
        assert "priority must -> could" in changed and "disposition via_needs -> deferred" in changed
        assert "criteria: removed two" in changed
        need = by_kind["need_changed"][0]["detail"]
        assert "search terms: removed oauth / added zzz" in need
        assert "problem types: removed oauth-flow" in need
        assert "serves: added fresh" in need
        assert "constraints: removed" in need and "license_allowlist" in need

    def test_a_difference_nothing_itemises_is_still_reported(self):
        s = self.snapshot([_req("a")], [])
        other = {**s, "schema": "requirements/2"}
        assert [c["kind"] for c in requirements.diff_snapshots(s, other)] == ["other_change"]

    def test_wrong_types_and_oversize_are_refused_not_defaulted(self):
        doc = "# A\n\nUsers must sign in with Google.\n"
        base = {"text": "t", "quote": "Users must sign in with Google.",
                "acceptance_criteria": ["c"]}
        for field in ("kind", "priority", "disposition", "slug"):
            clean, errors = requirements.validate_requirement_draft({**base, field: ["could"]}, doc)
            assert clean is None and f"{field} must be a string" in errors
        clean, errors = requirements.validate_requirement_draft({**base, "slug": "s" * 81}, doc)
        assert clean is None and "slug is longer than 80" in errors[0]
        derived, _ = requirements.validate_requirement_draft({**base, "text": "word " * 60}, doc)
        assert len(derived["slug"]) <= 80 and derived["slug_explicit"] is False
        explicit, _ = requirements.validate_requirement_draft({**base, "slug": "Mine"}, doc)
        assert explicit["slug"] == "mine" and explicit["slug_explicit"] is True

    def test_need_drafts_refuse_lookalikes_and_bulk(self):
        need = {"description": "OAuth sign-in", "search_terms": ["oauth"], "satisfies": ["r"]}
        clean, errors, _ = requirements.validate_need_draft({**need, "search_terms": ["оauth"]})
        assert clean is None and "mixes alphabets" in errors[0] and "\\u043e" in errors[0]
        clean, errors, _ = requirements.validate_need_draft({**need, "constraints": [
            {"kind": "license_allowlist", "spdx_ids": [f"L-{i}" for i in range(400)]}]})
        assert clean is None and "larger than 1000 characters" in errors[0]
        clean, errors, _ = requirements.validate_need_draft({**need, "slug": 7})
        assert clean is None and "slug must be a string" in errors
        ok, errors, _ = requirements.validate_need_draft(need)
        assert errors == [] and ok["slug_explicit"] is False


class TestCipOnlyClaimsWhatItHasRead:
    """H1, H2, H4, M2. When a computed verdict carries CIP's authority."""

    LICENSED = dict(constraint_kinds=["license_allowlist"])

    def test_build_needs_evidence_for_every_candidate(self):
        partial = dict(self.LICENSED, computed_verdict="REJECT", winner_capability_version_id="a",
                       evaluated_capability_version_ids=["a", "b", "c"],
                       unanalysed_capability_version_ids=["b", "c"])
        refusal = classify(**partial)
        assert isinstance(refusal, decisions.DecisionRefusal)
        assert "no analysed evidence for one or more candidates" in refusal.reason
        judged = classify(**partial, proposed_verdict="BUILD", rationale="read them myself")
        assert judged.decided_by == "llm_judgment" and judged.computed_verdict is None
        assert decisions.WHY_INSUFFICIENT in judged.flags
        full = classify(**{**partial, "unanalysed_capability_version_ids": []})
        assert full.decided_by == "cip_rules" and full.final_verdict == "BUILD"

    def test_a_reuse_verdict_needs_evidence_for_the_one_chosen(self):
        chosen_read = classify(**self.LICENSED, computed_verdict="ADOPT",
                               computed_capability_version_id="a", winner_capability_version_id="a",
                               evaluated_capability_version_ids=["a", "b"],
                               unanalysed_capability_version_ids=["b"])
        assert chosen_read.decided_by == "cip_rules" and chosen_read.final_capability_version_id == "a"
        chosen_unread = classify(**self.LICENSED, computed_verdict="ADOPT",
                                 computed_capability_version_id="a",
                                 winner_capability_version_id="a",
                                 evaluated_capability_version_ids=["a", "b"],
                                 unanalysed_capability_version_ids=["a"])
        assert isinstance(chosen_unread, decisions.DecisionRefusal)

    def test_a_truncated_sweep_cannot_conclude_nothing_fits(self):
        seen_all = dict(self.LICENSED, computed_verdict="REJECT",
                        evaluated_capability_version_ids=["a"], winner_capability_version_id="a")
        assert classify(**seen_all).decided_by == "cip_rules"
        refusal = classify(**seen_all, sweep_truncated=True)
        assert isinstance(refusal, decisions.DecisionRefusal) and "ceiling" in refusal.reason
        judged = classify(**seen_all, sweep_truncated=True, proposed_verdict="BUILD", rationale="r")
        assert judged.decided_by == "llm_judgment"
        assert {decisions.FLAG_TRUNCATED, decisions.WHY_TRUNCATED} <= set(judged.flags)
        assert "ceiling" in judged.note

    def test_truncation_does_not_block_a_reuse_verdict(self):
        out = classify(**self.LICENSED, computed_verdict="ADOPT", computed_capability_version_id="a",
                       winner_capability_version_id="a", evaluated_capability_version_ids=["a"],
                       sweep_truncated=True)
        assert out.decided_by == "cip_rules" and decisions.FLAG_TRUNCATED not in out.flags

    def test_no_candidates_is_only_forced_when_the_sweep_saw_everything(self):
        forced = classify()
        assert forced.decided_by == "cip_rules" and forced.flags == (decisions.FLAG_COLD_START,)
        refusal = classify(sweep_truncated=True, unevaluable_count=200)
        assert isinstance(refusal, decisions.DecisionRefusal) and "ceiling" in refusal.reason
        judged = classify(sweep_truncated=True, unevaluable_count=200,
                          proposed_verdict="BUILD", rationale="r")
        assert judged.decided_by == "llm_judgment"

    def test_matches_that_could_not_be_evaluated_are_not_nothing(self):
        co = decisions.coverage_outcome
        assert co(evaluated_count=0, evaluation_hit_count=0) == "no_registry_coverage"
        assert co(evaluated_count=0, evaluation_hit_count=2) == "banked_only"
        assert co(evaluated_count=0, evaluation_hit_count=2,
                  unevaluable_count=1) == "found_but_unevaluable"
        assert co(evaluated_count=1, evaluation_hit_count=2,
                  unevaluable_count=1) == "candidates_evaluated"
        out = classify(unevaluable_count=3)
        assert out.coverage_outcome == "found_but_unevaluable"
        assert out.flags == (decisions.FLAG_UNEVALUABLE,)

    def test_a_real_gap_is_not_hidden_behind_missing_evidence(self):
        both = classify(**self.LICENSED, computed_verdict="REJECT", winner_capability_version_id="a",
                        evaluated_capability_version_ids=["a"],
                        unanalysed_capability_version_ids=["a"],
                        blocking_gap_capability_version_ids=["a"],
                        proposed_verdict="ADOPT", proposed_capability_version_id="a", rationale="r")
        assert {decisions.FLAG_UNANALYSED, decisions.FLAG_BLOCKING_GAP,
                decisions.WHY_INSUFFICIENT} <= set(both.flags)
        only_unread = classify(**self.LICENSED, computed_verdict="REJECT",
                               winner_capability_version_id="a",
                               evaluated_capability_version_ids=["a"],
                               unanalysed_capability_version_ids=["a"],
                               proposed_verdict="ADOPT", proposed_capability_version_id="a",
                               rationale="r")
        assert decisions.FLAG_BLOCKING_GAP not in only_unread.flags

    def test_the_reason_for_judgment_is_the_actual_one(self):
        no_constraint = classify(evaluated_capability_version_ids=["a"],
                                 proposed_verdict="BUILD", rationale="r")
        assert decisions.WHY_NO_CONSTRAINT in no_constraint.flags
        assert "no machine-checkable constraints" in no_constraint.note
        overridden = classify(**self.LICENSED, computed_verdict="ADOPT",
                              computed_capability_version_id="a",
                              evaluated_capability_version_ids=["a"],
                              proposed_verdict="BUILD", rationale="r")
        assert overridden.decided_by == "llm_override"
        assert not any(f.startswith("judgment:") for f in overridden.flags)


class TestLockSaysWhatItRestsOn:
    """H7, M2, M10 and the smaller lock findings."""

    def test_duplicate_cost_lines_and_odd_amounts_are_refused(self):
        _, errors = lock.validate_cost_commitments([
            {"item": "Hosting", "kind": "free", "amount": "0"},
            {"item": "hosting", "kind": "recurring", "amount": "20000", "currency": "usd"}])
        assert any("duplicate item 'hosting'" in e for e in errors)
        for amount in (["5"], {"v": 5}, True):
            _, errors = lock.validate_cost_commitments(
                [{"item": "x", "kind": "usage", "amount": amount}])
            assert errors == ["cost_commitments[0]: amount must be a number or a string"]
        clean, errors = lock.validate_cost_commitments(
            [{"item": "x", "kind": "usage", "amount": 12.5, "currency": "ttd", "period": "Month"}])
        assert errors == [] and clean[0] == {"item": "x", "kind": "usage", "amount": "12.5",
                                             "currency": "TTD", "period": "month"}

    @pytest.mark.parametrize("amount,flagged", [
        ("0", False), ("0.00", False), ("", False), ("free", False),
        ("5", True), ("2,400", True), ("1.000.000", True), ("$0.50", True),
        ("1e3", True), ("٥٠٠", True),
    ])
    def test_money_is_money_whatever_it_is_filed_under(self, amount, flagged):
        p = _payload(cost_commitments=[{"item": "x", "kind": "free", "amount": amount,
                                        "currency": "USD", "period": ""}])
        assert ("cost_commitment" in codes(p)) is flagged

    def test_a_known_provider_is_flagged_under_any_label(self):
        def flags(name, category):
            p = _payload(approved_providers=[{"name": name, "category": category, "purpose": "",
                                              "cost": "free", "data_leaves_infra": False}])
            return [f for f in lock.lock_flags(p) if f["code"] == "sensitive_provider"]
        hidden = flags("Stripe  Checkout", "other")
        assert len(hidden) == 1 and "known payment provider" in hidden[0]["detail"]
        assert len(flags("Stripe", "payment")) == 1
        assert flags("Acme Logs", "other") == []
        assert lock.known_provider_category("Azure  OpenAI") == "ai"
        assert lock.known_provider_category("") is None

    def test_each_reason_cip_stood_back_has_its_own_flag(self):
        def detail(**over):
            p = _payload(decisions=[_decision(verdict="BUILD", **over)])
            return {f["code"]: f["detail"] for f in lock.lock_flags(p)}
        assert "could not evaluate" in detail(flags=[decisions.FLAG_UNEVALUABLE])[
            "unevaluable_candidates"]
        assert "reached its ceiling" in detail(flags=[decisions.FLAG_TRUNCATED])["sweep_truncated"]
        assert "evaluation_replaced" in detail(flags=[decisions.FLAG_BANKED_CHANGED])[
            "banked_evaluation_changed"]
        for why, words in ((decisions.WHY_INSUFFICIENT, "lacked the evidence"),
                           (decisions.WHY_TRUNCATED, "did not see every match"),
                           (decisions.WHY_NO_CONSTRAINT, "no machine-checkable constraints"),
                           ("", "did not compute a verdict")):
            got = detail(decided_by="llm_judgment", flags=[why] if why else [])
            assert words in got["llm_judgment"]

    def test_a_license_says_where_it_was_read(self):
        def detail(source):
            p = _payload(decisions=[_decision(license="GPL-3.0", license_source=source)])
            return next(f["detail"] for f in lock.lock_flags(p)
                        if f["code"] == "non_permissive_license")
        assert "read from its source" in detail("evidence")
        assert "registry metadata, not read from source" in detail("metadata")
        assert "source not recorded" in detail(None)

    def test_the_architecture_document_is_named_in_the_payload(self):
        named = _payload(architecture_doc_ref="/work/ARCH.md", architecture_doc_file_hash="f" * 64)
        assert named["architecture_doc_ref"] == "/work/ARCH.md"
        assert lock.payload_hash(named) != lock.payload_hash(_payload())
        none = _payload(architecture_doc_hash=None, architecture_doc_ref="/work/ARCH.md",
                        architecture_doc_file_hash="f" * 64)
        assert none["architecture_doc_ref"] is None and none["architecture_doc_file_hash"] is None

    def summary(self, payload, old=None):
        return lock.render_lock_summary(
            project_name="clinic", next_version=2 if old else 1, payload=payload,
            flags=lock.lock_flags(payload), diff=lock.diff_payloads(old, payload))

    def test_the_summary_shows_what_each_decision_rests_on(self):
        p = _payload(decisions=[
            _decision("auth", license="MIT", license_source="evidence",
                      search_terms=["oauth", "oidc login"]),
            _decision("pdf", verdict="REFERENCE", license_source="metadata"),
            _decision("queue", verdict="BUILD", coverage_outcome="found_but_unevaluable")],
            architecture_doc_ref="/work/ARCH.md", architecture_doc_file_hash="ab" * 32)
        out = self.summary(p)
        assert ("3 capability need(s): 1 reused as a dependency, 1 built using a reference, "
                "1 built. The registry had candidates CIP could evaluate for 2 of 3.") in out
        assert "    pinned: o/auth @abc123" in out
        assert "    license: MIT (read from source); cost tier: free" in out
        assert "    license: MIT (registry metadata); cost tier: free" in out
        assert '    registry: candidates evaluated; searched for: "oauth", "oidc login"' in out
        assert "    registry: found but unevaluable; searched for: not recorded" in out
        doc = out.split("## Architecture document")[1]
        assert "- File: /work/ARCH.md" in doc
        assert f"- SHA-256 of that file when read: {'ab' * 32}" in doc

    def test_the_summary_says_when_the_document_was_not_a_file(self):
        inline = self.summary(_payload(architecture_doc_ref="inline"))
        assert "- TEXT SUPPLIED INLINE BY THE AGENT, not read from a file" in inline
        assert "- none supplied" in self.summary(_payload(architecture_doc_hash=None))
        assert "- source not recorded" in self.summary(_payload())

    # -- the relock diff ----------------------------------------------------

    def kinds(self, old, new):
        return [(c["kind"], c["subject"]) for c in lock.diff_payloads(old, new)["changes"]]

    def test_identical_content_is_the_only_thing_called_no_change(self):
        assert self.kinds(_payload(), _payload(replaces_lock_version=1)) == []
        assert "- none: the content is identical" in self.summary(
            _payload(replaces_lock_version=1), old=_payload())

    @pytest.mark.parametrize("field,value,words", [
        ("license", "GPL-3.0", "license GPL-3.0"),
        ("license_source", "metadata", "(metadata)"),
        ("cost_tier", "paid", "cost tier paid"),
        ("decided_by", "llm_override", "llm_override"),
        ("flags", ["overrides_blocking_gap"], "overrides_blocking_gap"),
        ("coverage_outcome", "banked_only", "registry banked_only"),
        ("pinned_revision", "def456", "@def456"),
        ("pinned_source_asset", "fork/auth", "from fork/auth"),
    ])
    def test_every_part_of_a_decision_shows_when_it_changes(self, field, value, words):
        changes = lock.diff_payloads(_payload(), _payload(decisions=[_decision(**{field: value})]))
        assert [c["kind"] for c in changes["changes"]] == ["decision_changed"]
        before, after = changes["changes"][0]["detail"].split("  ->  ")
        assert words in after and before != after

    def test_what_a_decision_serves_and_searched_shows_too(self):
        old = _payload(decisions=[_decision(search_terms=["oauth"])])
        new = _payload(decisions=[_decision(search_terms=["zzz"], satisfies=["r1", "r2"])])
        changes = lock.diff_payloads(old, new)["changes"]
        assert [c["kind"] for c in changes] == ["decision_changed"]
        assert "serves: r1  ->  r1, r2" in changes[0]["detail"]
        assert "searched for: oauth  ->  zzz" in changes[0]["detail"]
        extra = lock.diff_payloads(_payload(), _payload(decisions=[_decision(new_field="x")]))
        assert "also changed: new_field" in extra["changes"][0]["detail"]

    def test_the_same_outcome_decided_again_is_a_change(self):
        again = _payload(decisions=[_decision(decision_record_id="dr-later")])
        assert self.kinds(_payload(), again) == [("decision_rerecorded", "auth")]

    def test_everything_else_in_the_lock_is_compared_in_full(self):
        provider = {"name": "Stripe", "category": "payment", "purpose": "cards",
                    "cost": "usage", "data_leaves_infra": True}
        cost = {"item": "hosting", "kind": "recurring", "amount": "20", "currency": "USD",
                "period": "month"}
        pd = {"key": "database", "category": "database", "choice": "Postgres", "rationale": ""}
        old = _payload(approved_providers=[provider], cost_commitments=[cost],
                       project_decisions=[pd], unresolved_risks=["a", "b"],
                       architecture_doc_ref="/a/ARCH.md")
        new = _payload(
            approved_providers=[{**provider, "purpose": "cards and payouts"}],
            cost_commitments=[{**cost, "amount": "2000"}],
            project_decisions=[{**pd, "rationale": "team knows it"}],
            unresolved_risks=["b", "c"], architecture_doc_ref="/b/ARCH.md",
            scope={"in_scope": [], "bespoke": [], "deferred": ["r1"], "not_applicable": []})
        got = self.kinds(old, new)
        assert got == [("project_decision_changed", "database"), ("provider_changed", "stripe"),
                       ("cost_changed", "hosting"), ("risk_removed", "risk"),
                       ("risk_added", "risk"), ("scope_changed", "deferred"),
                       ("scope_changed", "in scope"), ("architecture_doc_changed", "architecture")]
        details = " | ".join(c["detail"] for c in lock.diff_payloads(old, new)["changes"])
        assert "for: cards  ->  payment, usage, data leaves infrastructure, for: cards and payouts" \
            in details
        assert "recurring 20 USD per month  ->  recurring 2000 USD per month" in details
        assert "same content, read from a different place: /a/ARCH.md  ->  /b/ARCH.md" in details

    def test_added_and_removed_items(self):
        provider = {"name": "Stripe", "category": "payment", "purpose": "",
                    "cost": "usage", "data_leaves_infra": True}
        assert self.kinds(_payload(), _payload(approved_providers=[provider])) == [
            ("provider_added", "stripe")]
        assert self.kinds(_payload(approved_providers=[provider]), _payload()) == [
            ("provider_removed", "stripe")]
        two_needs = _payload(decisions=[_decision(), _decision("pdf", verdict="BUILD")])
        assert self.kinds(_payload(), two_needs) == [("decision_added", "pdf")]
        assert self.kinds(two_needs, _payload()) == [("decision_removed", "pdf")]

    def test_a_difference_nothing_itemises_is_still_reported(self):
        future = {**_payload(), "schema": "architecture-lock/9"}
        assert self.kinds(_payload(), future) == [("other_change", "lock")]
        both = {**_payload(unresolved_risks=["new"]), "retention_days": 30}
        assert self.kinds(_payload(), both) == [("risk_added", "risk"), ("other_change", "lock")]


class TestLockCheckSecondRoundRules:
    """M1, M9 and the size and legacy-row findings."""

    REFERENCE = LockView(
        version=1, stale_reasons=(),
        decisions={
            "pdf": {"need_id": "n-pdf", "verdict": "REFERENCE", "capability_version_id": "cv9",
                    "capability_key": "pypi:weasyprint", "pinned_revision": "abc"},
            "auth": {"need_id": "n-auth", "verdict": "WRAP", "capability_version_id": "cv1",
                     "capability_key": "pypi:authlib", "pinned_revision": "abc"},
        },
        approved_providers=(),
    )

    def test_a_reference_is_not_permission_to_install(self):
        out = check({"dependency_adds": [{"ecosystem": "PyPI", "name": "WeasyPrint"}]},
                    lock_view=self.REFERENCE)
        assert out.verdict == "needs_approval" and "locked as REFERENCE" in out.reason
        ok = check({"dependency_adds": [{"ecosystem": "pypi", "name": "authlib"}]},
                   lock_view=self.REFERENCE)
        assert ok.verdict == "allowed"

    @pytest.mark.parametrize("change,where", [
        ({"dependency_adds": [{"ecosystem": "pypi", "name": ["authlib"]}]}, "dependency_adds[0]"),
        ({"provider_changes": [{"name": "Google", "action": 1}]}, "provider_changes[0]"),
        ({"capability_changes": [{"need": "auth", "verdict": {"v": "ADOPT"}}]},
         "capability_changes[0]"),
        ({"project_decision_changes": [{"key": "database", "choice": 5}]},
         "project_decision_changes[0]"),
    ])
    def test_a_value_of_the_wrong_type_is_not_read_as_nothing(self, change, where):
        out = check(change)
        assert out.verdict == "needs_approval"
        assert where in out.reason and "must be text (or null)" in out.reason

    def test_an_explicit_null_still_means_none(self):
        out = check({"capability_changes": [{"need": "reports", "capability_version_id": None}]})
        assert out.verdict == "allowed"

    def test_a_change_too_large_to_be_a_change_is_blocked(self):
        big = {"changed_paths": ["src/auth/" + "x" * 50] * 500, "build_task_id": "t1"}
        out = check(big)
        assert out.verdict == "blocked" and "larger than 20000 characters" in out.reason
        assert check({"changed_paths": [object()]}).verdict in ("needs_approval", "blocked")

    def test_what_is_echoed_back_is_bounded_and_one_line(self):
        out = check({"changed_paths": ["src/auth/x.py"], f"k{ESC}[2J\n" + "y" * 5000: 1})
        assert out.verdict == "needs_approval"
        assert ESC not in out.reason and "\n" not in out.reason and len(out.reason) < 600

    def test_a_pattern_that_permits_everything_permits_nothing(self):
        old_task = TASK.__class__(id="t0", title="Old", status="in_progress", is_current=True,
                                  permitted_paths=("**", "src/auth/"), prohibited_changes=())
        inside = check({"changed_paths": ["src/auth/x.py"]}, task=old_task)
        assert inside.verdict == "needs_approval" and "'**'" in inside.reason
        only_wild = TASK.__class__(id="t0", title="Old", status="in_progress", is_current=True,
                                   permitted_paths=("**",), prohibited_changes=())
        outside = check({"changed_paths": ["deploy/prod.yml"]}, task=only_wild)
        assert "none usable" in outside.reason

    def test_work_on_a_drifted_need_is_on_hold(self):
        drifted = TASK.__class__(id="t1", title="Sign-in", status="in_progress", is_current=True,
                                 permitted_paths=("src/auth/",), prohibited_changes=(),
                                 drifted_needs=("auth",))
        out = check({"changed_paths": ["src/auth/x.py"]}, task=drifted)
        assert out.verdict == "needs_approval"
        assert "rests on auth" in out.reason and "on hold until a relock" in out.reason


class TestEvidenceHasLimits:

    def test_evidence_fields_are_capped(self):
        ok = {"outcome": "pass", "criterion_id": "c1", "commit_sha": "abc1234",
              "test_ref": "tests/x.py::t", "command": "pytest", "exit_code": 0}
        assert tasks.validate_verification(ok)[1] == []
        for field, limit in (("output_digest", 128), ("evidence_ref", 500)):
            clean, errors = tasks.validate_verification({**ok, field: "d" * (limit + 1)})
            assert clean is None and errors == [f"{field} is longer than {limit} characters"]
            assert tasks.validate_verification({**ok, field: "d" * limit})[1] == []
        assert tasks.MAX_REPORTS_PER_CALL == 100

    def test_a_reference_is_not_counted_as_reuse(self):
        from tests.test_pdr_core import cov_need
        out = coverage.compute_coverage(
            span_coverage={"unmapped_sections": [], "out_of_scope_only_sections": [{"heading": "S"}],
                           "lightly_quoted_sections": [{"heading": "T", "quoted_pct": 4}]},
            requirements=[cov_req("e", "via_needs", needs=["a", "b", "c", "d"])],
            needs=[cov_need("a", "ADOPT"), cov_need("b", "WRAP"), cov_need("c", "REFERENCE"),
                   cov_need("d", "BUILD")],
            tasks=[], lock=None)
        assert out["needs"]["reuse_pct"] == 50 and out["needs"]["reference_count"] == 1
        assert out["pdr"]["out_of_scope_only_sections"] == [{"heading": "S"}]
        assert out["pdr"]["lightly_quoted_sections"] == [{"heading": "T", "quoted_pct": 4}]


# ===========================================================================
# Third independent review. Pure-logic side; the end-to-end versions are in
# test_pdr_review3_regressions.py.
# ===========================================================================

class TestWhatCannotBeSeen:
    """H3, L1. Characters that draw nothing, and what is left of a field without them."""

    @pytest.mark.parametrize("ch", ["︎", "️", "\U000e0100", "᠋", "­",
                                    "⁤", "\U00016fe4", "\U0001d159", "​", "⠀"])
    def test_invisible(self, ch):
        assert text.invisible(ch)
        assert text.one_line(f"oau{ch}th") == "oau th"
        assert not text.is_plain_line(f"oau{ch}th")
        assert text.is_blank(ch * 3)

    @pytest.mark.parametrize("ch", ["a", "é", "́", " ", "-", "中", "\U0001f600"])
    def test_visible(self, ch):
        assert not text.invisible(ch)

    @pytest.mark.parametrize("value,blank", [
        ("", True), ("   ", True), ("́́", True), ("️ ́", True),
        ("a", False), ("-", False), ("7", False), ("é", False), ("—", False),
    ])
    def test_a_field_needs_something_to_look_at(self, value, blank):
        assert text.is_blank(value) is blank

    @pytest.mark.parametrize("term", ["oauth", "c++", "node.js", "ci/cd", "oidc login", "F#",
                                      "pdf-render", "s3", "oauth 2.0"])
    def test_a_search_term_that_is_one(self, term):
        assert text.search_term_problem(term) is None

    @pytest.mark.parametrize("term,why", [
        ("oauth, oidc, sso", "contains ','"), ("a;b", "contains ';'"), ("100%", "contains '%'"),
        ("snake_case", "contains '_'"), ("it's", 'contains "\'"'), ('"oauth"', "contains"),
        ("oau︎th", "invisible character (U+FE0E)"), ("óauth", "U+0301"),
        ("оauth", "mixes alphabets"), ("oauth\tsso", "U+0009"), ("a(b)", "contains '('"),
    ])
    def test_a_search_term_that_is_not(self, term, why):
        assert why in text.search_term_problem(term)

    @pytest.mark.parametrize("name", ["Stripe", "Str︎ipe", "ＳＴＲＩＰＥ",
                                      "s t r i p e", "STRIPE, Inc.", "Stripé", "stripe-payments"])
    def test_a_name_folded_for_matching(self, name):
        assert "stripe" in text.fold_name(name)
        assert lock.known_provider_category(name) == "payment"

    def test_folding_keeps_different_names_different(self):
        assert text.fold_name("Acme Logs") == "acmelogs"
        assert text.fold_name(None) == "" and text.fold_name(5) == "5"
        assert lock.known_provider_category("Acme Logs") is None

    def test_a_provider_name_is_plain_and_in_one_alphabet(self):
        ok = {"category": "other", "cost": "free", "data_leaves_infra": False}
        for bad in ("Str︎ipe", "Ѕtripe", "Stripe\nOpenAI", "a​b"):
            _, errors = lock.validate_providers([{**ok, "name": bad}])
            assert errors and "plain text in one alphabet" in errors[0], repr(bad)
        _, errors = lock.validate_providers([{**ok, "name": "Stripe"}, {**ok, "name": "S-T-R-I-P-E"}])
        assert any("duplicate provider" in e for e in errors)
        clean, errors = lock.validate_providers([{**ok, "name": "Acme Logs"}])
        assert errors == [] and clean[0]["name"] == "Acme Logs"


class TestWrappingReadsLikeProse:

    def test_ordinary_text_breaks_between_words(self):
        line = "- WAIT for the human: run the approvals command in a terminal and do not run it yourself"
        rows = text.wrap_block(line, 40).split("\n")
        assert all(len(r) <= 40 for r in rows) and len(rows) >= 3
        assert all(r.startswith(text.WRAP_PREFIX) for r in rows[1:])
        assert all(r.endswith(" ") for r in rows[:-1])              # broke after a space
        assert "".join(r[len(text.WRAP_PREFIX):] if i else r for i, r in enumerate(rows)) == line

    def test_a_field_with_no_spaces_is_cut_where_it_stands(self):
        line = "- risk: " + "x" * 100
        rows = text.wrap_block(line, 40).split("\n")
        assert rows[0] == line[:40] and all(len(r) <= 40 for r in rows)


class TestMoreLayoutsThirdRound:
    """M9, L5."""

    def test_a_document_quoted_from_an_email(self):
        doc = ("> # Shop PDR\n>\n> ## Checkout\n> Customers pay by card.\n>\n"
               "> ## Refunds\n> Refunds within 30 days.\n")
        secs = spans.split_sections(doc)
        assert [s.heading for s in secs] == ["Shop PDR", "Checkout", "Refunds"]
        checkout = secs[1]
        assert doc[checkout.start:checkout.end].startswith("> ## Checkout")
        assert doc[checkout.body_start:checkout.end].startswith("> Customers pay by card.")

    def test_numbered_titles_with_no_blank_line_when_they_are_all_there_is(self):
        doc = ("Shop PDR\n1. Scope\nThe shop sells books online to customers.\n2. Billing\n"
               "Invoices are sent monthly by email.\n3. Audit\nEvery change is logged.\n")
        assert headings(doc) == ["(preamble)", "1. Scope", "2. Billing", "3. Audit"]

    def test_a_numbered_list_is_still_a_list(self):
        """The fallback does not turn a list with nothing under its items into sections."""
        doc = "1. First do this thing\n2. Then do that thing\n3. Finally do the other\n"
        assert len(spans.split_sections(doc)) == 1

    def test_out_of_scope_quotes_do_not_pad_the_quoted_share(self):
        rows = "\n".join(f"- Requirement number {i} must hold." for i in range(30))
        doc = f"# Table\n\n{rows}\n\n# Small\n\nOne short line here.\n"
        loc = spans.QuoteLocator(doc)
        one = loc.locate("Requirement number 3 must hold.")
        four = spans.Span(loc.locate("Requirement number 10 must hold.").start,
                          loc.locate("Requirement number 19 must hold.").end)
        alone = spans.span_coverage(doc, [one], sections=loc.sections)
        padded = spans.span_coverage(doc, [one], out_of_scope_spans=[four], sections=loc.sections)
        assert alone["lightly_quoted_sections"] == padded["lightly_quoted_sections"]
        assert padded["lightly_quoted_sections"][0]["quoted_pct"] < 10


class TestRulesOverEveryCandidate:
    """H1."""

    BASE = dict(constraint_kinds=["license_allowlist"], computed_verdict="REJECT",
                winner_capability_version_id="a", evaluated_capability_version_ids=["a", "b"])

    def test_build_is_not_cips_while_the_rules_accept_another(self):
        refusal = classify(**self.BASE, rule_accepted_capability_version_ids=["b"])
        assert isinstance(refusal, decisions.DecisionRefusal)
        assert "would accept another one" in refusal.reason and "(b)" in refusal.reason
        build = classify(**self.BASE, rule_accepted_capability_version_ids=["b"],
                         proposed_verdict="BUILD", rationale="r")
        assert build.decided_by == "llm_judgment" and build.computed_verdict is None
        assert {decisions.FLAG_OTHER_ACCEPTED, decisions.WHY_OTHER_ACCEPTED} <= set(build.flags)
        reuse = classify(**self.BASE, rule_accepted_capability_version_ids=["b"],
                         proposed_verdict="ADAPT", proposed_capability_version_id="b",
                         rationale="r")
        assert reuse.decided_by == "llm_judgment" and reuse.final_capability_version_id == "b"
        assert decisions.FLAG_OTHER_ACCEPTED not in reuse.flags

    def test_it_is_cips_when_the_rules_accept_none(self):
        out = classify(**self.BASE, rule_accepted_capability_version_ids=[])
        assert out.decided_by == "cip_rules" and out.final_verdict == "BUILD"
        assert "rejected every candidate" in out.note
        ghost = classify(**self.BASE, rule_accepted_capability_version_ids=["not-evaluated"])
        assert ghost.decided_by == "cip_rules"

    def test_a_reuse_verdict_is_not_affected(self):
        out = classify(constraint_kinds=["license_allowlist"], computed_verdict="ADAPT",
                       computed_capability_version_id="a", winner_capability_version_id="a",
                       evaluated_capability_version_ids=["a", "b"],
                       rule_accepted_capability_version_ids=["a", "b"])
        assert out.decided_by == "cip_rules" and out.final_verdict == "ADAPT"

    def test_an_override_to_build_over_an_acceptable_candidate_is_flagged(self):
        out = classify(constraint_kinds=["license_allowlist"], computed_verdict="ADOPT",
                       computed_capability_version_id="a", winner_capability_version_id="a",
                       evaluated_capability_version_ids=["a"],
                       rule_accepted_capability_version_ids=["a"],
                       proposed_verdict="BUILD", rationale="r")
        assert out.decided_by == "llm_override"
        assert decisions.FLAG_OTHER_ACCEPTED in out.flags

    def test_the_lock_says_so(self):
        p = _payload(decisions=[_decision(verdict="BUILD", decided_by="llm_judgment",
                                          flags=[decisions.FLAG_OTHER_ACCEPTED,
                                                 decisions.WHY_OTHER_ACCEPTED])])
        flags = {f["code"]: f["detail"] for f in lock.lock_flags(p)}
        assert "would accept a candidate" in flags["build_over_acceptable_candidate"]
        assert "would accept another" in flags["llm_judgment"]


class TestRequestHistoryInThePayload:
    """H6, L2 and the override that never reached the human."""

    def test_where_a_payload_sits_in_history_is_hashed_but_is_not_content(self):
        first, later = _payload(), _payload(follows_lock_request="req-1")
        assert later["follows_lock_request"] == "req-1"
        assert lock.payload_hash(first) != lock.payload_hash(later)
        assert lock.content_core_hash(first) == lock.content_core_hash(later)
        assert lock.content_core_hash(first) == lock.content_core_hash(
            _payload(replaces_lock_version=3, follows_lock_request="req-9"))
        assert lock.diff_payloads(first, later)["changes"] == []

    def test_a_change_never_reads_x_to_x(self):
        swap = lock.diff_payloads(
            _payload(), _payload(decisions=[_decision(capability_version_id="cv-other")]))
        detail = swap["changes"][0]["detail"]
        before, after = detail.split("  ->  ")
        assert before != after and "[capability_version_id: cv-auth -> cv-other]" in after
        provider = {"name": "stripe", "category": "payment", "purpose": "", "cost": "usage",
                    "data_leaves_infra": True}
        renamed = lock.diff_payloads(_payload(approved_providers=[provider]),
                                     _payload(approved_providers=[{**provider, "name": "Stripe"}]))
        assert "[name: stripe -> Stripe]" in renamed["changes"][0]["detail"]

    def test_a_stored_lock_that_cannot_be_compared_still_says_something(self):
        for broken in ({**_payload(), "approved_providers": [None, 3]},
                       {**_payload(), "unresolved_risks": [["nested"]]},
                       {**_payload(), "scope": {"in_scope": None, "bespoke": 5}},
                       {**_payload(), "decisions": "not a list"}):
            changes = lock.diff_payloads(broken, _payload())["changes"]
            assert changes, broken
        assert lock.diff_payloads({"decisions": [{"need_slug": 1}]}, _payload())["changes"]

    def test_the_summary_shows_what_cip_computed_and_the_agents_reason(self):
        p = _payload(decisions=[
            _decision("auth", verdict="BUILD", decided_by="llm_override", computed_verdict="ADOPT",
                      computed_capability_key="pypi:authlib", rationale="client wants its own"),
            _decision("pdf", decided_by="llm_judgment", computed_verdict=None,
                      rationale="read the license myself"),
            _decision("queue", decided_by="cip_rules", computed_verdict="ADOPT",
                      rationale="")])
        out = lock.render_lock_summary(project_name="p", next_version=1, payload=p,
                                       flags=lock.lock_flags(p),
                                       diff=lock.diff_payloads(None, p))
        assert "    CIP computed: ADOPT of pypi:authlib" in out
        assert "    agent's reason: client wants its own" in out
        assert "    CIP computed: nothing it would stand behind" in out
        assert "    agent's reason: read the license myself" in out
        assert out.count("agent's reason:") == 2          # not for the cip_rules decision
        changed = lock.diff_payloads(p, _payload(decisions=[
            {**d, "rationale": "another reason"} if d["need_slug"] == "auth" else d
            for d in p["decisions"]]))
        assert changed["changes"] == [{"kind": "decision_changed", "subject": "auth",
                                       "detail": "also changed: rationale"}]


class TestDriftInTheLockCheck:
    """M4."""

    DRIFTED = LockView(
        version=2, stale_reasons=(), approved_providers=(),
        decisions={
            "auth": {"need_id": "n-auth", "verdict": "ADOPT", "capability_version_id": "cv1",
                     "capability_key": "pypi:authlib", "pinned_revision": "abc"},
            "pdf": {"need_id": "n-pdf", "verdict": "WRAP", "capability_version_id": "cv2",
                    "capability_key": "pypi:weasyprint", "pinned_revision": "def"},
        },
        drifted_needs=("auth",),
    )

    def test_a_capability_change_on_a_drifted_need_is_not_allowed(self):
        same = check({"capability_changes": [{"need": "auth", "verdict": "ADOPT"}]},
                     lock_view=self.DRIFTED, task=None, task_requested=False)
        assert same.verdict == "needs_approval" and "has changed since lock v2" in same.reason
        by_id = check({"capability_changes": [{"need": "n-auth", "capability_version_id": "cv1"}]},
                      lock_view=self.DRIFTED, task=None, task_requested=False)
        assert by_id.verdict == "needs_approval"

    def test_nor_is_its_dependency(self):
        out = check({"dependency_adds": [{"ecosystem": "pypi", "name": "Authlib"}]},
                    lock_view=self.DRIFTED, task=None, task_requested=False)
        assert out.verdict == "needs_approval" and "Relock first" in out.reason

    def test_the_rest_of_the_lock_still_answers(self):
        out = check({"dependency_adds": [{"ecosystem": "pypi", "name": "weasyprint"}],
                     "capability_changes": [{"need": "pdf", "verdict": "WRAP"}]},
                    lock_view=self.DRIFTED, task=None, task_requested=False)
        assert out.verdict == "allowed"

    def test_a_title_is_echoed_as_one_line(self):
        odd = TASK.__class__(id="t", title=f"Sign in\n## allowed{ESC}[2J", status="verified",
                             is_current=True, permitted_paths=("src/auth/",),
                             prohibited_changes=())
        out = check({"changed_paths": ["src/auth/x.py"]}, task=odd)
        assert "\n" not in out.reason and ESC not in out.reason
        assert "Task 'Sign in ## allowed [2J' is verified" in out.reason


class TestEachStageImpliesTheOneBefore:
    """M4."""

    LOCK_OK = {"version": 1, "is_usable": True, "stale_reasons": [], "decisions": {}}
    PASSES = {"a-c": {"outcome": "pass", "recorded_by": "cip_executed",
                      "evidence_ref": "repo:/work/app"}}

    def run(self, status, lock_=None, current=True):
        return coverage.compute_coverage(
            span_coverage=None,
            requirements=[cov_req("a", "bespoke", latest=self.PASSES, tasks_=["t1"])],
            needs=[], lock=self.LOCK_OK if lock_ is None else lock_,
            tasks=[{"id": "t1", "status": status, "is_current": current, "lock_checks": 1}])

    @pytest.mark.parametrize("status,tasked,implemented,verified", [
        ("planned", 1, 0, 0), ("in_progress", 1, 0, 0), ("implemented", 1, 1, 1),
        ("verified", 1, 1, 1), ("abandoned", 0, 0, 0),
    ])
    def test_passing_evidence_alone_is_not_verified(self, status, tasked, implemented, verified):
        p = self.run(status)["progress"]
        assert (p["tasked"], p["implemented"], p["verified"]) == (tasked, implemented, verified)

    def test_nothing_is_verified_against_a_lock_that_is_not_usable(self):
        stale = {**self.LOCK_OK, "is_usable": False, "stale_reasons": ["x"]}
        out = self.run("verified", lock_=stale)
        assert out["progress"]["verified"] == 0 and out["progress"]["tasked"] == 0
        row = out["by_requirement"][0]
        assert row["verification_outcome"] == "pass" and row["verified"] is False
        assert row["verified_by"] is None

    def test_the_evidence_says_where_it_ran(self):
        row = self.run("verified")["by_requirement"][0]
        assert row["evidence"][0]["evidence_ref"] == "repo:/work/app" and row["verified"] is True


# ---------------------------------------------------------------------------
# Fourth review. Pure rules; the end-to-end side is in
# test_pdr_review4_regressions.py.
# ---------------------------------------------------------------------------

class TestATermIsWrittenInAscii:
    """H4. A whole word in look-alikes passed the rule against mixing alphabets."""

    @pytest.mark.parametrize("term,codepoint", [
        ("\U0001d5c8\U0001d5ba\U0001d5ce\U0001d5cd\U0001d5c1", "U+1D5C8"),      # mathematical
        ("ｏａｕｔｈ", "U+FF4F"),                            # fullwidth
        ("сасһе", "U+0441"),                            # Cyrillic "cache"
        ("ѕѕо", "U+0455"),                                        # Cyrillic "sso"
        ("ɡraphql", "U+0261"), ("oɑuth", "U+0251"), ("oıdc", "U+0131"),
        ("café", "U+00E9"), ("платёж", "U+043F"),
        ("हिंदी", "U+0939"), ("oauth login", "U+00A0"),
    ])
    def test_anything_outside_ascii_is_refused_and_named(self, term, codepoint):
        problem = text.search_term_problem(term)
        assert "plain ASCII" in problem and codepoint in problem and problem.isascii()

    def test_the_older_reasons_are_still_the_ones_given(self):
        assert "mixes alphabets" in text.search_term_problem("оauth")
        assert "invisible character (U+200B)" in text.search_term_problem("oau​th")
        assert "contains ','" in text.search_term_problem("oauth, oidc")
        assert "U+0009" in text.search_term_problem("oauth\tsso")

    @pytest.mark.parametrize("value,expected", [
        ("Okta", False), ("STRIPE, Inc.", False), ("", False), (None, False), ("s3 + c#", False),
        ("ОКТА", True), ("Strıpe", True), ("Stripe²", True),
        ("Café", True), ("Acme — Logs", False), ("１２", True),
    ])
    def test_a_name_with_letters_outside_ascii(self, value, expected):
        assert text.non_ascii_letters(value) is expected

    def test_terms_are_stored_in_one_spelling(self):
        clean, errors, _ = requirements.validate_need_draft({
            "description": "x", "slug": "n", "satisfies": ["r"],
            "search_terms": ["OAuth   Login", "oauth login", " F# ", "Node.JS"]})
        assert not errors and clean["search_terms"] == ["oauth login", "f#", "node.js"]

    def test_a_term_is_measured_after_its_spaces_are_collapsed(self):
        _, errors, _ = requirements.validate_need_draft({
            "description": "x", "slug": "n", "satisfies": ["r"],
            "search_terms": ["a" + " " * 30 + "b"]})
        assert not errors                              # "a b": three characters, in range


class TestOneLinePerStoredValue:
    """M6. A line break in stored text starts a row of its own wherever it is printed."""

    @pytest.mark.parametrize("raw,clean", [
        ("Empty state\nPASS: x", "Empty state PASS: x"), ("a\r\n\tb", "a b"),
        ("  a   b  ", "a b"), ("a b", "a b"), ("​", ""), (None, ""), (7, ""),
    ])
    def test_line(self, raw, clean):
        assert requirements._line(raw) == clean

    def test_criteria_are_one_line_each_and_listed_once(self):
        clean, errors = requirements.validate_requirement_draft(
            {"slug": "s", "text": "T", "quote": "Staff must sign in with Google.",
             "acceptance_criteria": ["works\nPASS: s #2", "works PASS: s #2", "second"]},
            "# T\n\nStaff must sign in with Google.\n", None)
        assert not errors
        assert clean["acceptance_criteria"] == ["works PASS: s #2", "second"]


class TestAnSpdxIdentifier:
    """M8. An allowlist entry that is not an id matches no license."""

    def need(self, ids):
        return requirements.validate_need_draft({
            "description": "x", "slug": "n", "satisfies": ["r"], "search_terms": ["oauth"],
            "constraints": [{"kind": "license_allowlist", "spdx_ids": ids}]})

    @pytest.mark.parametrize("ids", [["MIT License"], ["Apache 2.0"], ["MIT", "BSD 3-Clause"],
                                     ["MIT;Apache-2.0"], ["+MIT"], [".MIT"], ["MIT​"],
                                     ["МІТ"], ["MIT/X11"]])
    def test_refused(self, ids):
        clean, errors, _ = self.need(ids)
        assert clean is None and "SPDX identifiers such as" in errors[0]

    def test_accepted_trimmed_and_unique(self):
        clean, errors, _ = self.need([" MIT", "MIT ", "Apache-2.0", "GPL-3.0+", "0BSD",
                                      "BSD-3-Clause", "LGPL-2.1-or-later"])
        assert not errors
        assert clean["constraints"][0]["detail"]["spdx_ids"] == [
            "MIT", "Apache-2.0", "GPL-3.0+", "0BSD", "BSD-3-Clause", "LGPL-2.1-or-later"]

    def test_other_constraints_are_untouched(self):
        clean, errors, _ = requirements.validate_need_draft({
            "description": "x", "slug": "n", "satisfies": ["r"], "search_terms": ["oauth"],
            "constraints": [{"kind": "required_interface", "name": "get token"}]})
        assert not errors and clean["constraints"][0]["detail"]["name"] == "get token"


class TestTheQuotedPassageHasACeiling:
    """L6. Found with whitespace collapsed, the passage can outgrow the quote."""

    def test_measured_as_it_stands_in_the_document(self):
        words = ["Staff", "must", "sign", "in", "with", "Google", "today."]
        doc = "# T\n\n" + (" " * 120).join(words) + "\n"
        clean, errors = requirements.validate_requirement_draft(
            {"slug": "s", "text": "T", "quote": " ".join(words),
             "acceptance_criteria": ["works"]}, doc, None)
        assert clean is None and "as it stands in the PDR" in errors[0]

    def test_a_passage_at_the_ceiling_is_fine(self):
        sentence = "Staff must sign in with Google. " + "x" * (spans.MAX_QUOTE_CHARS - 33) + "."
        assert len(sentence) == spans.MAX_QUOTE_CHARS
        clean, errors = requirements.validate_requirement_draft(
            {"slug": "s", "text": "T", "quote": sentence, "acceptance_criteria": ["works"]},
            "# T\n\n" + sentence + "\n", None)
        assert not errors and clean["span"]["end"] - clean["span"]["start"] == spans.MAX_QUOTE_CHARS


class TestARationaleSomeoneCanRead:
    """L4. An override 'explained' with a zero-width space."""
    OVERRIDE = dict(constraint_kinds=["license_allowlist"], computed_verdict="ADOPT",
                    computed_capability_version_id="cv1", proposed_verdict="BUILD",
                    evaluated_capability_version_ids=["cv1"])

    @pytest.mark.parametrize("rationale", ["", "   ", "\u200b", "\ufeff\u200d", "\u0301", None])
    def test_nothing_visible_is_no_rationale(self, rationale):
        out = classify(**self.OVERRIDE, rationale=rationale)
        assert isinstance(out, decisions.DecisionRefusal) and "rationale is required" in out.reason

    def test_a_real_one_is_accepted(self):
        out = classify(**self.OVERRIDE, rationale="  we own this  ")
        assert not isinstance(out, decisions.DecisionRefusal)
        assert out.decided_by == "llm_override"
