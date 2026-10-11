"""
Requirement and capability-need validation, snapshots and readiness.

Two kinds of row share project_requirement:

  PDR requirement (functional | non_functional | constraint)
      What the PDR asks for. Has a source quote, a priority, acceptance
      criteria and a disposition.

  Capability need (capability_need)
      A reusable capability the build needs — auth, a job queue, PDF
      rendering. This is what gets searched and decided, because a
      sentence like "the receptionist can see today's bookings" has no
      interface, license or dependency for a fit matcher to work on.

A requirement reaches a decision one of four ways (its disposition):
  via_needs      — through the capability needs linked to it
  bespoke        — project-specific logic; nothing reusable to search for
  not_applicable — not a build item
  deferred       — out of this build, on the record

Everything here is pure. Validation returns (clean, errors); it never
raises on bad input from the driving LLM.
"""
from __future__ import annotations

import re
from typing import Any

from core.pdr.canonical import canonical_json, content_hash, text_hash
from core.pdr.spans import MAX_QUOTE_CHARS, MIN_QUOTE_CHARS, QuoteLocator
from core.pdr.text import is_blank, nfc, search_term_problem
from core.project.requirements import (
    MATCHED_CONSTRAINT_KINDS,
    RequirementParseError,
    parse_constraint,
)

PRIORITIES = ("must", "should", "could", "wont")
REQUIREMENT_KINDS = ("functional", "non_functional", "constraint")
NEED_KIND = "capability_need"
DISPOSITIONS = ("via_needs", "bespoke", "not_applicable", "deferred")
# What record_requirements may set directly. via_needs is set only by
# linking a capability need — it cannot be asserted.
DIRECT_DISPOSITIONS = ("bespoke", "not_applicable", "deferred")
IN_SCOPE_DISPOSITIONS = ("via_needs", "bespoke")

MAX_SEARCH_TERMS = 8
MAX_PROBLEM_TYPES = 12
SEARCH_TERM_MIN, SEARCH_TERM_MAX = 2, 60
# Everything the agent writes that a human later reads in an approval
# summary is capped, so the summary can show it in full. A field that is
# hashed but truncated on screen would be approved unseen.
MAX_FIELD_CHARS = 500
MAX_CRITERIA = 20
MAX_SLUG_CHARS = 80
MAX_CONSTRAINT_CHARS = 1000


def slugify(text: str) -> str:
    return _slug_full(text)[:MAX_SLUG_CHARS].strip("-")


def _slug_full(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower().strip()).strip("-")


# ---------------------------------------------------------------------------
# PDR requirement drafts
# ---------------------------------------------------------------------------

def validate_requirement_draft(
    raw: Any, pdr_text: str | None, locator: QuoteLocator | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """
    Validate one requirement submitted by the driving LLM.

    Returns (clean, errors). `clean` is None when there are errors.
    The source span is computed here from the quote; any offsets the
    caller supplied are ignored. Pass a QuoteLocator for the PDR when
    validating many drafts against one text.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["requirement must be an object"]

    text = _line(raw.get("text"))
    if not text:
        errors.append("text is required")

    # A value of the wrong type is an error, not a silent default: a
    # priority sent as ["could"] must not be recorded as "must".
    for field in ("kind", "priority", "disposition", "slug"):
        if raw.get(field) is not None and not isinstance(raw.get(field), str):
            errors.append(f"{field} must be a string")

    kind = _s(raw.get("kind")) or "functional"
    if kind == NEED_KIND:
        errors.append(
            "capability needs are recorded with record_capability_needs, "
            "not record_requirements"
        )
    elif kind not in REQUIREMENT_KINDS:
        errors.append(f"kind must be one of {list(REQUIREMENT_KINDS)}, got '{kind}'")

    priority = _s(raw.get("priority")) or "must"
    if priority not in PRIORITIES:
        errors.append(f"priority must be one of {list(PRIORITIES)}, got '{priority}'")

    disposition = _s(raw.get("disposition")) or None
    reason = _line(raw.get("disposition_reason")) or None
    if disposition == "via_needs":
        errors.append(
            "disposition 'via_needs' cannot be set directly; it is set when a "
            "capability need is linked with record_capability_needs"
        )
    elif disposition is not None and disposition not in DIRECT_DISPOSITIONS:
        errors.append(
            f"disposition must be one of {list(DIRECT_DISPOSITIONS)} or omitted, "
            f"got '{disposition}'"
        )
    elif disposition is not None and not reason:
        errors.append(f"disposition '{disposition}' requires disposition_reason")

    if raw.get("constraints"):
        errors.append(
            "constraints belong on capability needs (record_capability_needs), "
            "not on PDR requirements"
        )

    criteria = _criteria(raw.get("acceptance_criteria"))
    # Anything that will be built must be verifiable, whatever its kind:
    # an in-scope requirement with no criterion could never be verified,
    # and the project could never be complete.
    if disposition not in ("deferred", "not_applicable") and not criteria:
        errors.append(
            "acceptance_criteria is required (at least one testable criterion) "
            "unless the requirement is deferred or not_applicable"
        )
    if len(criteria) > MAX_CRITERIA:
        errors.append(f"at most {MAX_CRITERIA} acceptance criteria per requirement; split it")
    for label, value in (("text", text), ("disposition_reason", reason or ""),
                         *((f"acceptance_criteria[{i}]", c) for i, c in enumerate(criteria))):
        if len(value) > MAX_FIELD_CHARS:
            errors.append(f"{label} is longer than {MAX_FIELD_CHARS} characters")

    # Quote → span. Accept the v1.0 shape (source_span.quote) too.
    quote = _s(raw.get("quote"))
    span_in = raw.get("source_span")
    if not quote and isinstance(span_in, dict):
        quote = _s(span_in.get("quote"))
    hint = None
    if isinstance(raw.get("start_hint"), int):
        hint = raw["start_hint"]
    elif isinstance(span_in, dict) and isinstance(span_in.get("start"), int):
        hint = span_in["start"]

    span = None
    if not quote:
        errors.append(
            "quote is required: the verbatim PDR text this requirement comes from "
            "(CIP computes the offsets)"
        )
    elif pdr_text is None:
        errors.append(
            "the PDR text for this source is not stored (ingested before v1.1); "
            "re-run ingest_pdr so quotes can be checked"
        )
    elif len(quote) > MAX_QUOTE_CHARS:
        errors.append(
            f"quote is longer than {MAX_QUOTE_CHARS} characters. Quote the sentence "
            f"that states this requirement, not the passage around it"
        )
    else:
        if locator is None or locator.text != pdr_text:
            locator = QuoteLocator(pdr_text)
        span = locator.locate(quote, hint)
        if span is None:
            if len(re.sub(r"\s+", " ", quote)) < MIN_QUOTE_CHARS:
                errors.append(f"quote is too short to anchor (minimum {MIN_QUOTE_CHARS} characters)")
            else:
                errors.append(
                    "quote not found in the PDR text; it must be copied verbatim "
                    "(whitespace may differ, wording may not)"
                )
        elif span.end - span.start > MAX_QUOTE_CHARS:
            # Matched with whitespace collapsed, so the passage in the PDR
            # can be longer than the quote that found it.
            errors.append(
                f"the quoted passage is longer than {MAX_QUOTE_CHARS} characters as it "
                f"stands in the PDR. Quote the sentence that states this requirement"
            )
        else:
            crossed = locator.heading_inside(span)
            if crossed is not None:
                errors.append(
                    f"quote runs across the heading '{crossed[:60]}' into another section. "
                    f"One requirement comes from one place: quote the sentence that states it"
                )

    explicit_slug = _s(raw.get("slug"))
    if explicit_slug and len(_slug_full(explicit_slug)) > MAX_SLUG_CHARS:
        errors.append(f"slug is longer than {MAX_SLUG_CHARS} characters")

    if errors:
        return None, errors

    slug = slugify(explicit_slug or text)
    if not slug:
        return None, ["could not derive a slug from text; supply slug"]

    return {
        "slug": slug,
        # False when CIP derived the slug from the text. A derived slug that
        # collides with a different requirement must not silently overwrite it.
        "slug_explicit": bool(explicit_slug),
        "text": text,
        "kind": kind,
        "priority": priority,
        "acceptance_criteria": criteria,
        "disposition": disposition,
        "disposition_reason": reason if disposition else None,
        "quote": pdr_text[span.start:span.end],
        "span": {"start": span.start, "end": span.end},
    }, []


# ---------------------------------------------------------------------------
# Capability need drafts
# ---------------------------------------------------------------------------

_SPACES = re.compile(r" {2,}")
_SPDX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")


def validate_need_draft(raw: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """
    Validate one capability need. Returns (clean, errors, warnings).

    `satisfies` is returned as given (slugs or ids); the orchestrator
    resolves it against the project.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return None, ["need must be an object"], []

    description = _line(raw.get("description")) or _line(raw.get("text"))
    if not description:
        errors.append("description is required")
    elif len(description) > MAX_FIELD_CHARS:
        errors.append(f"description is longer than {MAX_FIELD_CHARS} characters")
    for field in ("slug", "description"):
        if raw.get(field) is not None and not isinstance(raw.get(field), str):
            errors.append(f"{field} must be a string")
    explicit_slug = _s(raw.get("slug"))
    if explicit_slug and len(_slug_full(explicit_slug)) > MAX_SLUG_CHARS:
        errors.append(f"slug is longer than {MAX_SLUG_CHARS} characters")
    slug = slugify(explicit_slug or description)
    if not slug:
        errors.append("could not derive a slug; supply slug")

    # One spelling per term: single spaces, lower case. The search is not
    # case-sensitive, so case carries nothing except the chance to pass a
    # capital I for a small l; and "oauth  2" with two spaces is displayed
    # as "oauth 2" by anything that collapses spaces and matches neither.
    terms = list(dict.fromkeys(
        _SPACES.sub(" ", t).lower() if t.isascii() else t
        for t in _unique_strs(raw.get("search_terms"))))
    if not terms:
        errors.append(
            "search_terms is required: 1-8 short keyword phrases the registry is "
            "searched with (e.g. 'oauth', 'pdf render')"
        )
    elif len(terms) > MAX_SEARCH_TERMS:
        errors.append(f"at most {MAX_SEARCH_TERMS} search_terms")
    else:
        bad = [t for t in terms if not (SEARCH_TERM_MIN <= len(t) <= SEARCH_TERM_MAX)]
        if bad:
            errors.append(
                f"search_terms must be {SEARCH_TERM_MIN}-{SEARCH_TERM_MAX} characters "
                f"each; offending: {bad}"
            )
        # What the person confirms has to be what the registry is asked.
        # "оauth" with a Cyrillic о reads as "oauth" and matches nothing;
        # "oauth, oidc, sso" as one term reads as three and finds what none
        # of them would.
        for t in terms:
            problem = search_term_problem(t)
            if problem:
                errors.append(
                    f"search term {t.encode('unicode_escape').decode('ascii')!r} {problem}"
                )

    problem_types = [slugify(t) for t in _unique_strs(raw.get("problem_types"))]
    problem_types = [t for t in dict.fromkeys(problem_types) if t]
    if len(problem_types) > MAX_PROBLEM_TYPES:
        errors.append(f"at most {MAX_PROBLEM_TYPES} problem_types")

    satisfies = _unique_strs(raw.get("satisfies"))
    if not satisfies:
        errors.append(
            "satisfies is required: the slugs (or ids) of the PDR requirements "
            "this capability serves"
        )

    constraints: list[dict[str, Any]] = []
    raw_constraints = raw.get("constraints") or []
    if not isinstance(raw_constraints, list):
        errors.append("constraints must be a list")
    else:
        for i, c in enumerate(raw_constraints):
            try:
                parsed = parse_constraint(slug or "need", i, c)
            except RequirementParseError as e:
                errors.append(str(e))
                continue
            if parsed["kind"] == "license_allowlist":
                # Matched as written against the SPDX id read from the
                # source. "MIT License" or "Apache 2.0" matches no license
                # at all: every candidate CIP had read a license for then
                # failed the allowlist, and the BUILD that followed was
                # recorded as CIP's own finding.
                ids = [_line(x) for x in (parsed.get("detail") or {}).get("spdx_ids") or []]
                bad_ids = [x for x in ids if not _SPDX_ID.match(x)]
                if bad_ids or not ids:
                    errors.append(
                        f"constraints[{i}]: spdx_ids must be SPDX identifiers such as "
                        f"'MIT', 'Apache-2.0', 'BSD-3-Clause' (letters, digits, '.', '-', "
                        f"'+'; no spaces); not {bad_ids or ids}"
                    )
                    continue
                parsed = {**parsed, "detail": {**parsed["detail"],
                                               "spdx_ids": list(dict.fromkeys(ids))}}
            try:
                size = len(canonical_json(parsed.get("detail") or {}))
            except (TypeError, ValueError) as e:
                errors.append(f"constraints[{i}]: {e}")
                continue
            if size > MAX_CONSTRAINT_CHARS:
                errors.append(
                    f"constraints[{i}] is larger than {MAX_CONSTRAINT_CHARS} characters"
                )
                continue
            constraints.append(parsed)

    if errors:
        return None, errors, warnings

    if not any(c["kind"] in MATCHED_CONSTRAINT_KINDS for c in constraints):
        warnings.append(
            "no machine-checkable constraints: CIP can rank candidates for this "
            "need but cannot compute a verdict, so the decision will be recorded "
            "as LLM judgment and listed for the human at lock time"
        )

    return {
        "slug": slug,
        "slug_explicit": bool(explicit_slug),
        "description": description,
        "search_terms": terms,
        "problem_types": problem_types,
        "constraints": constraints,
        "satisfies": satisfies,
    }, [], warnings


def need_fingerprint(
    *, search_terms: list[str], constraints: list[dict],
    problem_types: list[str] | None = None,
) -> str:
    """
    Hash of what a search for this need actually depends on. A search
    logged under one fingerprint does not count once the terms, the
    constraints or the problem-type tags change. The tags are what the
    evaluation store is recalled by: a sweep run before a tag was added
    never looked for the banked assets that tag would have found.
    """
    return content_hash({
        "search_terms": sorted(t.lower() for t in search_terms),
        "constraints": _sorted_constraints(constraints),
        "problem_types": sorted(problem_types or []),
    })


# ---------------------------------------------------------------------------
# Snapshot — what the human confirms
# ---------------------------------------------------------------------------

def requirements_snapshot(
    *,
    source_content_hash: str | None,
    requirements: list[dict[str, Any]],
    needs: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Canonical description of the confirmed scope.

    requirements: active PDR requirements, each {slug, kind, priority,
        text, acceptance_criteria[], disposition, disposition_reason, quote}
    needs: active capability needs, each {slug, description,
        search_terms[], problem_types[], constraints[{kind, detail}],
        satisfies[requirement slugs]}

    Search terms and problem-type tags are part of the confirmed scope.
    They define the mandatory search: if they could be changed after
    confirmation, a need could be re-recorded with terms that match
    nothing and "decided" as a clean BUILD. Extra searching is still
    free — search_for_requirement takes a custom query — but the sweep
    runs the terms the human saw.
    """
    return {
        "schema": "requirements/1",
        "source_content_hash": source_content_hash,
        "requirements": sorted(
            (
                {
                    "slug": r["slug"],
                    "kind": r["kind"],
                    "priority": r["priority"],
                    "text": r["text"],
                    "acceptance_criteria": list(r.get("acceptance_criteria") or []),
                    "disposition": r.get("disposition"),
                    "disposition_reason": r.get("disposition_reason"),
                    "quote_hash": (text_hash(r["quote"])
                                   if isinstance(r.get("quote"), str) and r["quote"] else None),
                    # Where the quote sits. The same words can occur twice in a
                    # PDR; moving the anchor changes which section looks read.
                    "span": _span_pair(r.get("span")),
                }
                for r in requirements
            ),
            key=lambda r: r["slug"],
        ),
        "needs": sorted(
            (
                {
                    "slug": n["slug"],
                    "description": n["description"],
                    "search_terms": sorted(t.lower() for t in n.get("search_terms") or []),
                    "problem_types": sorted(n.get("problem_types") or []),
                    "constraints": _sorted_constraints(n.get("constraints") or []),
                    "satisfies": sorted(n.get("satisfies") or []),
                }
                for n in needs
            ),
            key=lambda n: n["slug"],
        ),
    }


def requirements_hash(snapshot: dict[str, Any]) -> str:
    return content_hash(snapshot)


def diff_snapshots(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, str]]:
    """
    What changed between the scope a human last confirmed and the one now
    in front of them. Each entry is {kind, subject, detail}.

    A second confirmation that looks like the first is how a dropped
    must-have or a removed license constraint gets approved unseen: the
    list is long and nothing says which lines are different.
    """
    if not old:
        return []
    changes: list[dict[str, str]] = []

    def add(kind: str, subject: str, detail: str = "") -> None:
        changes.append({"kind": kind, "subject": subject, "detail": detail})

    if old.get("source_content_hash") != new.get("source_content_hash"):
        add("pdr_text_changed", "PDR", "the PDR text is not the one last confirmed")

    def by_slug(snap: dict, key: str) -> dict[str, dict]:
        return {x["slug"]: x for x in snap.get(key) or [] if isinstance(x, dict) and "slug" in x}

    def list_delta(label: str, before, after) -> str | None:
        before, after = list(before or []), list(after or [])
        if before == after:
            return None
        gone = [x for x in before if x not in after]
        came = [x for x in after if x not in before]
        parts = []
        if gone:
            parts.append("removed " + "; ".join(_show(x) for x in gone))
        if came:
            parts.append("added " + "; ".join(_show(x) for x in came))
        return f"{label}: " + (" / ".join(parts) or "reordered")

    old_r, new_r = by_slug(old, "requirements"), by_slug(new, "requirements")
    for slug in sorted(old_r.keys() - new_r.keys()):
        r = old_r[slug]
        add("requirement_removed", slug,
            f"[{r.get('priority')}] {r.get('text')} (was {r.get('disposition')})")
    for slug in sorted(new_r.keys() - old_r.keys()):
        r = new_r[slug]
        add("requirement_added", slug, f"[{r.get('priority')}] {r.get('text')}")
    for slug in sorted(old_r.keys() & new_r.keys()):
        a, b = old_r[slug], new_r[slug]
        parts: list[str] = []
        for field in ("priority", "kind", "disposition"):
            if a.get(field) != b.get(field):
                parts.append(f"{field} {a.get(field)} -> {b.get(field)}")
        if a.get("text") != b.get("text"):
            parts.append(f"text was: {a.get('text')}")
        if a.get("disposition_reason") != b.get("disposition_reason"):
            parts.append(f"reason was: {a.get('disposition_reason')}")
        delta = list_delta("criteria", a.get("acceptance_criteria"), b.get("acceptance_criteria"))
        if delta:
            parts.append(delta)
        if a.get("quote_hash") != b.get("quote_hash"):
            parts.append("quotes a different part of the PDR")
        elif "span" in a and a.get("span") != b.get("span"):
            parts.append("its quote is anchored at a different place in the PDR")
        if parts:
            add("requirement_changed", slug, "; ".join(parts))

    old_n, new_n = by_slug(old, "needs"), by_slug(new, "needs")
    for slug in sorted(old_n.keys() - new_n.keys()):
        add("need_removed", slug, str(old_n[slug].get("description") or ""))
    for slug in sorted(new_n.keys() - old_n.keys()):
        add("need_added", slug, str(new_n[slug].get("description") or ""))
    for slug in sorted(old_n.keys() & new_n.keys()):
        a, b = old_n[slug], new_n[slug]
        parts = []
        if a.get("description") != b.get("description"):
            parts.append(f"description was: {a.get('description')}")
        for label, field in (("search terms", "search_terms"), ("problem types", "problem_types"),
                             ("serves", "satisfies"), ("constraints", "constraints")):
            delta = list_delta(label, a.get(field), b.get(field))
            if delta:
                parts.append(delta)
        if parts:
            add("need_changed", slug, "; ".join(parts))
    if not changes and content_hash(old) != content_hash(new):
        add("other_change", "scope",
            "the scope differs from the one last confirmed in a way not itemised here; "
            "read the whole summary")
    return changes


def _show(x: Any) -> str:
    return x if isinstance(x, str) else canonical_json(x)


def _span_pair(span: Any) -> list[int] | None:
    if (isinstance(span, dict) and isinstance(span.get("start"), int)
            and isinstance(span.get("end"), int)):
        return [span["start"], span["end"]]
    return None


def confirmation_blockers(
    *,
    latest_source_id: str | None,
    requirements: list[dict[str, Any]],
    needs: list[dict[str, Any]],
) -> list[str]:
    """
    Why the scope cannot be put in front of the human yet.

    requirements additionally carry {source_id, has_span};
    needs carry {satisfies[...]}.
    """
    out: list[str] = []
    if latest_source_id is None:
        out.append("no PDR has been ingested for this project")
    if not requirements:
        out.append("no requirements recorded")
        return out

    need_slugs_by_req: dict[str, list[str]] = {}
    for n in needs:
        if not n.get("satisfies"):
            out.append(
                f"need '{n['slug']}' is linked to no active requirement; link it or retire it"
            )
        for r_slug in n.get("satisfies") or []:
            need_slugs_by_req.setdefault(r_slug, []).append(n["slug"])

    for r in sorted(requirements, key=lambda r: r["slug"]):
        slug = r["slug"]
        disp = r.get("disposition")
        if r.get("span_issue") == "not_in_text":
            out.append(
                f"requirement '{slug}': its recorded quote is not what the PDR says at "
                f"that place; re-record it with a verbatim quote"
            )
        elif not r.get("has_span"):
            out.append(f"requirement '{slug}' has no source quote; re-record it with a quote")
        elif r.get("span_issue") == "crosses_heading":
            out.append(
                f"requirement '{slug}': its quote runs across a section heading of the "
                f"current PDR; re-record it with the one sentence that states it"
            )
        elif r.get("span_issue") == "too_long":
            out.append(
                f"requirement '{slug}': its quote is longer than {MAX_QUOTE_CHARS} "
                f"characters; re-record it with the one sentence that states it"
            )
        elif latest_source_id is not None and r.get("source_id") != latest_source_id:
            out.append(
                f"requirement '{slug}' is anchored to a superseded PDR; its quote was "
                f"not found in the current text — re-record it or retire it"
            )
        if disp is None:
            out.append(
                f"requirement '{slug}' has no disposition: link a capability need, or "
                f"mark it bespoke, not_applicable or deferred with a reason"
            )
        elif disp == "via_needs" and not need_slugs_by_req.get(slug):
            out.append(
                f"requirement '{slug}' is via_needs but has no active capability need linked"
            )
        if disp in IN_SCOPE_DISPOSITIONS and not r.get("acceptance_criteria"):
            out.append(
                f"requirement '{slug}' is in scope but has no acceptance criteria, "
                f"so it could never be verified"
            )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s(v: Any) -> str:
    """
    A clean string, or "". Composed to NFC so one name has one spelling,
    and empty if nothing in it would be visible: a field made of
    zero-width characters is not a reason, a criterion or a requirement.
    """
    if not isinstance(v, str) or is_blank(v):
        return ""
    return nfc(v).strip()


_WS = re.compile(r"\s+")


def _line(v: Any) -> str:
    """
    _s() on one line: every run of whitespace, line breaks included, is a
    single space. For text that is stored and later printed in a row of
    its own ("still unverified: <criterion>"): a line break inside it
    starts a new row, and what follows reads as something CIP printed. A
    quote is not passed through here; it has to match the PDR as written.
    """
    return _WS.sub(" ", _s(v))


def _criteria(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out = [_line(x) for x in v]
    return [x for x in dict.fromkeys(out) if x]


def _unique_strs(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    seen: dict[str, str] = {}
    for x in v:
        s = _s(x)
        if s and s.lower() not in seen:
            seen[s.lower()] = s
    return list(seen.values())


def _sorted_constraints(constraints: list[dict]) -> list[dict]:
    items = [{"kind": c["kind"], "detail": c.get("detail") or {}} for c in constraints]
    return sorted(items, key=canonical_json)
