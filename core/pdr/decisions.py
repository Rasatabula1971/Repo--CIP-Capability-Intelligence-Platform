"""
Decision authority — who decides a capability need, and on what basis.

The v1.0 contract had the driving LLM pass in the verdict, while the
spec said CIP decides. This module settles it:

  1. CIP always computes first, through the Step 12 rules profile
     (workers.recommend). Nothing here re-implements the rules.

  2. Whether that computed verdict means anything depends on two things.

     The need. core.project.fit scores a candidate against CONSTRAINTS.
     A need with no machine-checkable constraint gives every candidate
     fit_score 1.0, so the rules would "decide" on text match and
     intrinsic score alone.

     The candidate. The fit matchers read EVIDENCE: license evidence,
     extracted interfaces, declared dependencies. A component ingested
     from registry metadata alone (most GitHub-search and awesome-list
     rows) has none. Against it, a license allowlist reports "no
     identified license" as a blocking gap, a required interface is
     always missing, and a forbidden dependency is always "absent". Those
     are statements about missing evidence, not findings about the code.

     Neither is a decision CIP can stand behind.

  3. So each recorded decision is labelled with who actually made it:

       cip_rules     CIP's computed verdict stands. Either the fit was
                     informative and the proposal agreed (or none was
                     given), or the search found no candidates at all.
       llm_override  The fit was informative and the LLM proposes
                     something else. Needs a rationale; listed for the
                     human at lock time.
       llm_judgment  The fit was not informative: the need has no
                     checkable constraint, or the best-ranked candidate
                     was never analysed. The LLM's verdict is recorded as
                     its own judgment, not CIP's; listed for the human at
                     lock time.

Need-level final verdicts are ADOPT, ADAPT, WRAP, REFERENCE and BUILD.
REJECT is a statement about candidates ("none of these work"), not an
outcome for a need: a need whose candidates are all rejected still has
to be satisfied, so the outcome is BUILD. A need nobody wants is
retired, not rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.pdr.text import is_blank
from core.project.requirements import MATCHED_CONSTRAINT_KINDS

VERDICTS = ("ADOPT", "ADAPT", "WRAP", "REFERENCE", "REJECT", "BUILD")
FINAL_VERDICTS = ("ADOPT", "ADAPT", "WRAP", "REFERENCE", "BUILD")
REUSE_VERDICTS = ("ADOPT", "ADAPT", "WRAP", "REFERENCE")
# Reuse verdicts that make the capability a dependency of the build.
DEPENDENCY_VERDICTS = ("ADOPT", "ADAPT", "WRAP")

FLAG_COLD_START = "cold_start_build"
FLAG_BANKED_MATCH = "build_despite_banked_match"
FLAG_BLOCKING_GAP = "overrides_blocking_gap"
FLAG_UNANALYSED = "unanalysed_candidate"
FLAG_UNEVALUABLE = "unevaluable_candidates"
FLAG_TRUNCATED = "sweep_truncated"
FLAG_BANKED_CHANGED = "banked_evaluation_changed"
FLAG_OTHER_ACCEPTED = "build_over_acceptable_candidate"
# Why CIP did not compute the verdict itself. Carried on the decision so
# the human is told the actual reason, not a generic one.
WHY_NO_CONSTRAINT = "judgment:no_checkable_constraint"
WHY_INSUFFICIENT = "judgment:insufficient_evidence"
WHY_TRUNCATED = "judgment:sweep_truncated"
WHY_OTHER_ACCEPTED = "judgment:rules_accept_another_candidate"


@dataclass(frozen=True)
class DecisionOutcome:
    final_verdict: str
    final_capability_version_id: str | None
    decided_by: str                         # cip_rules | llm_override | llm_judgment
    computed_verdict: str | None            # None when the fit was not informative
    computed_capability_version_id: str | None
    coverage_outcome: str
    flags: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


@dataclass(frozen=True)
class DecisionRefusal:
    reason: str


def fit_is_informative(constraint_kinds: list[str]) -> bool:
    """True when at least one constraint has a matcher in core.project.fit."""
    return any(k in MATCHED_CONSTRAINT_KINDS for k in constraint_kinds)


def coverage_outcome(
    *, evaluated_count: int, evaluation_hit_count: int, unevaluable_count: int = 0,
) -> str:
    """
    What the registry was able to say about a need.

      candidates_evaluated  at least one registry candidate was fit-evaluated
      found_but_unevaluable the registry returned matches, and none could be
                            evaluated (no source binding to pin). Not the
                            same as having nothing: ingesting them would
                            make them candidates
      banked_only           nothing in the registry, but the evaluation
                            store holds a matching banked asset
      no_registry_coverage  CIP had nothing. A BUILD here was not "chosen
                            over reuse" — the question was never answerable
    """
    if evaluated_count > 0:
        return "candidates_evaluated"
    if unevaluable_count > 0:
        return "found_but_unevaluable"
    if evaluation_hit_count > 0:
        return "banked_only"
    return "no_registry_coverage"


def classify_decision(
    *,
    constraint_kinds: list[str],
    computed_verdict: str,
    computed_capability_version_id: str | None,
    proposed_verdict: str | None,
    proposed_capability_version_id: str | None,
    evaluated_capability_version_ids: list[str],
    blocking_gap_capability_version_ids: list[str],
    evaluation_hit_count: int,
    rationale: str,
    winner_capability_version_id: str | None = None,
    unanalysed_capability_version_ids: list[str] | None = None,
    unevaluable_count: int = 0,
    sweep_truncated: bool = False,
    rule_accepted_capability_version_ids: list[str] | None = None,
) -> DecisionOutcome | DecisionRefusal:
    """
    Turn (what CIP computed, what the LLM proposes) into a recorded
    decision, or refuse.

    computed_verdict comes straight from workers.recommend. A computed
    REJECT is treated as BUILD for a need (see module docstring).

    winner_capability_version_id is the rank-1 candidate the rules were
    applied to (present even when the verdict is REJECT or BUILD).
    unanalysed_capability_version_ids are the evaluated candidates CIP
    lacks evidence for, on what this need's constraints check.
    blocking_gap_capability_version_ids are the candidates with a blocking
    gap that rests on evidence (a license that was read and is not
    allowed), as opposed to one that only records missing evidence.
    unevaluable_count is how many registry matches could not be evaluated.
    sweep_truncated is true when the mandatory sweep hit its ceiling, so
    there may be candidates it never looked at.

    When CIP's verdict carries authority:

      a reuse verdict   the need has a checkable constraint, and CIP holds
                        the evidence for the candidate it chose
      BUILD             it holds the evidence for EVERY candidate, the
                        sweep saw all of them, and the rules accept NONE of
                        them. "Nothing fits" is a claim about all of them.
                        One readable GPL library ranked above three unread
                        MIT ones is not grounds for it.

    rule_accepted_capability_version_ids are the evaluated candidates the
    rules profile would give a reuse verdict, each taken on its own. Step
    12 runs the rules on the best-ranked candidate only, and ranks on fit
    before intrinsic score: a perfect fit with a poor score is rejected
    while the candidate under it would have been ADAPT. That REJECT is a
    statement about one candidate.
    """
    evaluated = [str(x) for x in evaluated_capability_version_ids]
    unanalysed = {str(x) for x in (unanalysed_capability_version_ids or [])}
    winner = str(winner_capability_version_id) if winner_capability_version_id else None
    accepted = [str(x) for x in (rule_accepted_capability_version_ids or [])
                if str(x) in evaluated]
    # With no winner given, assume analysed: callers that predate the
    # distinction keep their behaviour.
    winner_analysed = winner is None or winner not in unanalysed
    has_candidates = bool(evaluated)
    # Blank means nothing visible: a zero-width space is not a reason.
    rationale = "" if is_blank(rationale) else str(rationale).strip()
    outcome = coverage_outcome(
        evaluated_count=len(evaluated), evaluation_hit_count=evaluation_hit_count,
        unevaluable_count=unevaluable_count,
    )

    if computed_verdict not in VERDICTS:
        return DecisionRefusal(f"CIP computed an unknown verdict: {computed_verdict!r}")

    proposed = (proposed_verdict or "").strip().upper() or None
    proposed_cv = str(proposed_capability_version_id) if proposed_capability_version_id else None

    if proposed == "REJECT":
        return DecisionRefusal(
            "REJECT is not an outcome for a capability need. If no candidate works, "
            "record BUILD; if the need itself is unwanted, retire it."
        )
    if proposed is not None and proposed not in FINAL_VERDICTS:
        return DecisionRefusal(
            f"Invalid verdict: {proposed}. Use one of {list(FINAL_VERDICTS)}."
        )

    # What CIP computed, in need-level terms.
    effective_computed = "BUILD" if computed_verdict == "REJECT" else computed_verdict
    effective_computed_cv = (
        str(computed_capability_version_id)
        if computed_capability_version_id and effective_computed in REUSE_VERDICTS
        else None
    )

    # With candidates on the table, CIP's computed verdict only carries
    # authority if the fit was informative (see the docstring). With no
    # candidates at all, BUILD is forced.
    informative = fit_is_informative(constraint_kinds)
    why_not: str | None = None
    if not has_candidates:
        # Nothing to choose from, so BUILD is forced — unless the sweep
        # stopped at its ceiling. Then "nothing" only describes the part
        # of the registry it looked at.
        cip_can_decide, why_not = (not sweep_truncated), (
            WHY_TRUNCATED if sweep_truncated else None)
    elif not informative:
        cip_can_decide, why_not = False, WHY_NO_CONSTRAINT
    elif effective_computed in REUSE_VERDICTS:
        chosen_ok = (effective_computed_cv or winner) not in unanalysed and winner_analysed
        cip_can_decide, why_not = chosen_ok, (None if chosen_ok else WHY_INSUFFICIENT)
    elif set(evaluated) & unanalysed:
        cip_can_decide, why_not = False, WHY_INSUFFICIENT
    elif sweep_truncated:
        cip_can_decide, why_not = False, WHY_TRUNCATED
    elif accepted:
        cip_can_decide, why_not = False, WHY_OTHER_ACCEPTED
    else:
        cip_can_decide = True

    if cip_can_decide:
        if proposed is None:
            final, final_cv, decided_by = effective_computed, effective_computed_cv, "cip_rules"
        elif proposed == effective_computed and (
            proposed not in REUSE_VERDICTS or proposed_cv in (None, effective_computed_cv)
        ):
            final, final_cv, decided_by = effective_computed, effective_computed_cv, "cip_rules"
        else:
            final, final_cv, decided_by = proposed, proposed_cv, "llm_override"
    else:
        if proposed is None:
            if why_not == WHY_NO_CONSTRAINT:
                return DecisionRefusal(
                    "CIP cannot compute a verdict for this need: it has no machine-checkable "
                    "constraints, so every candidate scores fit 1.0. Supply a verdict "
                    "with a rationale (recorded as LLM judgment), or add constraints to the "
                    "need and search again."
                )
            if why_not == WHY_OTHER_ACCEPTED:
                return DecisionRefusal(
                    "CIP cannot conclude that nothing fits: its rules reject the "
                    "best-ranked candidate, but would accept another one that was "
                    f"evaluated ({', '.join(accepted)}). Look at the candidates again "
                    "and supply a verdict with a rationale (recorded as LLM judgment)."
                )
            if why_not == WHY_TRUNCATED:
                return DecisionRefusal(
                    "CIP cannot conclude that nothing fits: the sweep reached its ceiling, "
                    "so there are matches it never evaluated. Narrow the need's search terms "
                    "(they need confirming again), or supply a verdict with a rationale "
                    "(recorded as LLM judgment)."
                )
            return DecisionRefusal(
                "CIP cannot compute a verdict for this need: it holds no analysed evidence "
                "for one or more candidates on what the need's constraints check, so their "
                "fit scores reflect missing evidence rather than findings. Supply a verdict "
                "with a rationale (recorded as LLM judgment), or run source analysis on the "
                "candidates and search again."
            )
        final, final_cv, decided_by = proposed, proposed_cv, "llm_judgment"

    # Validate the final shape.
    if final in REUSE_VERDICTS:
        if not final_cv:
            return DecisionRefusal(
                f"{final} requires chosen_capability_version_id."
            )
        if final_cv not in evaluated:
            return DecisionRefusal(
                f"capability_version {final_cv} was not evaluated by a search for this "
                f"need. Only candidates returned by search_for_requirement can be chosen."
            )
    else:
        if proposed_cv and decided_by != "cip_rules":
            return DecisionRefusal("BUILD does not take a chosen_capability_version_id.")
        final_cv = None

    if decided_by != "cip_rules" and not rationale:
        what = "an override of CIP's computed verdict" if decided_by == "llm_override" \
            else "a verdict CIP could not compute"
        return DecisionRefusal(f"rationale is required for {what}.")

    flags: list[str] = []
    if final == "BUILD" and outcome == "no_registry_coverage":
        flags.append(FLAG_COLD_START)
    if final == "BUILD" and unevaluable_count > 0:
        flags.append(FLAG_UNEVALUABLE)
    if final == "BUILD" and evaluation_hit_count > 0:
        flags.append(FLAG_BANKED_MATCH)
    if final == "BUILD" and sweep_truncated:
        flags.append(FLAG_TRUNCATED)
    if final == "BUILD" and accepted:
        flags.append(FLAG_OTHER_ACCEPTED)
    # Two separate facts, and a candidate can carry both: CIP has not read
    # everything the constraints depend on, and what it HAS read rules the
    # candidate out. The second must not be hidden behind the first.
    if final in REUSE_VERDICTS and final_cv in unanalysed:
        flags.append(FLAG_UNANALYSED)
    if final in REUSE_VERDICTS and final_cv in {str(x) for x in blocking_gap_capability_version_ids}:
        flags.append(FLAG_BLOCKING_GAP)
    if decided_by == "llm_judgment" and why_not:
        flags.append(why_not)

    note = ""
    if computed_verdict == "REJECT" and decided_by == "cip_rules":
        note = "CIP's rules rejected every candidate; recorded as BUILD for the need."
    elif not cip_can_decide:
        why = {
            WHY_NO_CONSTRAINT: "no machine-checkable constraints",
            WHY_INSUFFICIENT: "no analysed evidence for one or more candidates on what the "
                              "constraints check",
            WHY_TRUNCATED: "the sweep reached its ceiling and did not see every match",
            WHY_OTHER_ACCEPTED: "the rules reject the best-ranked candidate and would "
                                "accept another",
        }[why_not]
        note = (
            f"Fit was not informative ({why}); "
            "this verdict is LLM judgment, not a CIP-computed decision."
        )

    return DecisionOutcome(
        final_verdict=final,
        final_capability_version_id=final_cv,
        decided_by=decided_by,
        computed_verdict=computed_verdict if cip_can_decide else None,
        computed_capability_version_id=effective_computed_cv if cip_can_decide else None,
        coverage_outcome=outcome,
        flags=tuple(flags),
        note=note,
    )
