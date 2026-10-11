"""
Architecture lock — payload, flags, blockers, diff, summary.

The lock is a frozen, hashed record of what was approved. Its hash
covers everything a human signed off on, not just the per-need
decisions:

  - the confirmed requirements (by hash)
  - the architecture document (by hash), if one was supplied
  - one decision per capability need, with the decision_record it rests on
  - cross-cutting project decisions (database, hosting, auth model) that
    belong to no single requirement
  - approved external providers and cost commitments
  - what is explicitly out of scope, and the risks left open

Human-attention items are derived from STRUCTURED facts — a decision's
decided_by, a capability's license and cost tier, a provider's category.
The v1.0 brief matched keywords like "payment" against requirement text,
which both over- and under-flagged and could not be audited.

One approval covers the whole payload. The flags do not each need a
separate sign-off; they decide what the summary puts at the top.
"""
from __future__ import annotations

import unicodedata
from typing import Any

from core.pdr.canonical import canonical_json, content_hash
from core.pdr.text import (
    fold_name, is_blank, is_plain_line, mixed_script, nfc, non_ascii_letters, one_line,
)
from core.pdr.decisions import (
    DEPENDENCY_VERDICTS,
    FLAG_BANKED_CHANGED,
    FLAG_BANKED_MATCH,
    FLAG_BLOCKING_GAP,
    FLAG_COLD_START,
    FLAG_OTHER_ACCEPTED,
    FLAG_TRUNCATED,
    FLAG_UNANALYSED,
    FLAG_UNEVALUABLE,
    REUSE_VERDICTS,
    WHY_INSUFFICIENT,
    WHY_NO_CONSTRAINT,
    WHY_OTHER_ACCEPTED,
    WHY_TRUNCATED,
)

PAYLOAD_SCHEMA = "architecture-lock/1.1"

# Every free-text field in the payload is capped so the approval summary
# can print it in full. A field that is hashed but cut short on screen
# would be approved unseen.
MAX_NAME_CHARS = 80
MAX_TEXT_CHARS = 300
MAX_ITEMS = 50

PROVIDER_CATEGORIES = (
    "ai", "auth", "payment", "hosting", "database", "storage",
    "email", "messaging", "analytics", "other",
)
# Categories the spec lists as always needing a human decision.
SENSITIVE_PROVIDER_CATEGORIES = ("ai", "auth", "payment")
COST_KINDS = ("free", "free_tier", "usage", "recurring", "one_off")

PERMISSIVE_LICENSES = frozenset({
    "MIT", "MIT-0", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC",
    "0BSD", "UNLICENSE", "CC0-1.0", "ZLIB", "BSL-1.0", "PYTHON-2.0",
})
FREE_COST_TIERS = ("free", "free_tier")

# Providers whose category is not in doubt. The category on a provider is
# the agent's own label, and the attention flags are derived from it: a
# payment processor declared as "other" would raise none. This list is a
# floor under that, not a registry — an unlisted provider is still taken
# at its declared category.
KNOWN_PROVIDERS = {
    "ai": ("openai", "anthropic", "claude", "gemini", "mistral", "cohere", "groq",
           "azure openai", "bedrock", "vertex ai", "hugging face", "replicate",
           "elevenlabs", "deepgram", "perplexity", "openrouter", "together ai"),
    "auth": ("auth0", "okta", "clerk", "cognito", "firebase auth", "supabase auth",
             "workos", "keycloak", "google oauth", "entra", "onelogin"),
    "payment": ("stripe", "paypal", "braintree", "adyen", "square", "paddle",
                "lemon squeezy", "wipay", "checkout.com", "mollie", "razorpay"),
}

PROJECT_DECISION_CATEGORIES = (
    "database", "hosting", "auth_model", "architecture_style", "language",
    "framework", "data_residency", "deployment", "testing", "other",
)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def validate_providers(raw: Any) -> tuple[list[dict], list[str]]:
    """approved_providers: [{name, category, purpose, cost, data_leaves_infra}]"""
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["approved_providers must be a list"]
    if len(raw) > MAX_ITEMS:
        return [], [f"at most {MAX_ITEMS} approved_providers"]
    clean, errors, seen = [], [], set()
    for i, p in enumerate(raw):
        where = f"approved_providers[{i}]"
        if not isinstance(p, dict):
            errors.append(f"{where}: must be an object")
            continue
        name = _s(p.get("name"))
        category = _s(p.get("category")).lower()
        cost = _s(p.get("cost")).lower() or "free"
        leaves = p.get("data_leaves_infra")
        if not name:
            errors.append(f"{where}: name is required")
        elif not is_plain_line(name) or mixed_script(name):
            # The name is matched against the known-provider list and read
            # by a person. An invisible character or a look-alike letter in
            # it is a way past one of them.
            errors.append(
                f"{where}: name must be plain text in one alphabet per word "
                f"(no invisible or look-alike characters)"
            )
        if category not in PROVIDER_CATEGORIES:
            errors.append(f"{where}: category must be one of {list(PROVIDER_CATEGORIES)}")
        if cost not in COST_KINDS:
            errors.append(f"{where}: cost must be one of {list(COST_KINDS)}")
        if not isinstance(leaves, bool):
            errors.append(
                f"{where}: data_leaves_infra must be true or false "
                f"(does this provider receive project or user data?)"
            )
        if name and fold_name(name) in seen:
            errors.append(f"{where}: duplicate provider '{name}'")
        seen.add(fold_name(name))
        errors += _too_long(where, name=(name, MAX_NAME_CHARS),
                            purpose=(_s(p.get("purpose")), MAX_TEXT_CHARS))
        clean.append({
            "name": name, "category": category, "purpose": _s(p.get("purpose")),
            "cost": cost, "data_leaves_infra": bool(leaves),
        })
    return (sorted(clean, key=lambda p: p["name"].lower()), errors) if not errors else ([], errors)


def validate_cost_commitments(raw: Any) -> tuple[list[dict], list[str]]:
    """cost_commitments: [{item, kind, amount, currency, period}]"""
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["cost_commitments must be a list"]
    if len(raw) > MAX_ITEMS:
        return [], [f"at most {MAX_ITEMS} cost_commitments"]
    clean, errors, seen = [], [], set()
    for i, c in enumerate(raw):
        where = f"cost_commitments[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where}: must be an object")
            continue
        item = _s(c.get("item"))
        kind = _s(c.get("kind")).lower()
        # Two lines for one item is how a large one hides behind a small
        # one: the diff and the reader both key on the name.
        if item and item.lower() in seen:
            errors.append(f"{where}: duplicate item '{item}'; give each commitment its own line")
        seen.add(item.lower())
        amount = c.get("amount")
        if amount is not None and (isinstance(amount, bool)
                                   or not isinstance(amount, (str, int, float))):
            errors.append(f"{where}: amount must be a number or a string")
            continue
        if not item:
            errors.append(f"{where}: item is required")
        if kind not in COST_KINDS:
            errors.append(f"{where}: kind must be one of {list(COST_KINDS)}")
        errors += _too_long(
            where, item=(item, MAX_NAME_CHARS),
            amount=(str(c.get("amount") or ""), 40),
            currency=(_s(c.get("currency")), 12), period=(_s(c.get("period")), 24),
        )
        clean.append({
            "item": item, "kind": kind,
            # Amounts are strings on purpose: no floats in a hashed payload.
            "amount": _s(str(c.get("amount"))) if c.get("amount") is not None else "",
            "currency": _s(c.get("currency")).upper(),
            "period": _s(c.get("period")).lower(),
        })
    return (sorted(clean, key=lambda c: c["item"].lower()), errors) if not errors else ([], errors)


def validate_project_decisions(raw: Any) -> tuple[list[dict], list[str]]:
    """project_decisions: [{key, category, choice, rationale}] — cross-cutting."""
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["project_decisions must be a list"]
    if len(raw) > MAX_ITEMS:
        return [], [f"at most {MAX_ITEMS} project_decisions"]
    clean, errors, seen = [], [], set()
    for i, d in enumerate(raw):
        where = f"project_decisions[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{where}: must be an object")
            continue
        key = _s(d.get("key")).lower()
        category = _s(d.get("category")).lower() or "other"
        choice = _s(d.get("choice"))
        if not key:
            errors.append(f"{where}: key is required (e.g. 'database')")
        if not choice:
            errors.append(f"{where}: choice is required")
        if category not in PROJECT_DECISION_CATEGORIES:
            errors.append(
                f"{where}: category must be one of {list(PROJECT_DECISION_CATEGORIES)}"
            )
        if key and key in seen:
            errors.append(f"{where}: duplicate key '{key}'")
        seen.add(key)
        errors += _too_long(where, key=(key, MAX_NAME_CHARS), choice=(choice, MAX_TEXT_CHARS),
                            rationale=(_s(d.get("rationale")), MAX_TEXT_CHARS))
        clean.append({
            "key": key, "category": category, "choice": choice,
            "rationale": _s(d.get("rationale")),
        })
    return (sorted(clean, key=lambda d: d["key"]), errors) if not errors else ([], errors)


def validate_risks(raw: Any) -> tuple[list[str], list[str]]:
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
        return [], ["unresolved_risks must be a list of strings"]
    if len(raw) > MAX_ITEMS:
        return [], [f"at most {MAX_ITEMS} unresolved_risks"]
    long = [i for i, r in enumerate(raw) if len(r) > MAX_TEXT_CHARS]
    if long:
        return [], [f"unresolved_risks[{long[0]}] is longer than {MAX_TEXT_CHARS} characters"]
    return sorted({_s(r) for r in raw if _s(r)}), []


def _too_long(where: str, **fields: tuple[str, int]) -> list[str]:
    return [
        f"{where}: {name} is longer than {limit} characters"
        for name, (value, limit) in fields.items()
        if isinstance(value, str) and len(value) > limit
    ]


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def build_lock_payload(
    *,
    project_id: str,
    requirements_hash: str,
    architecture_doc_hash: str | None,
    decisions: list[dict[str, Any]],
    project_decisions: list[dict],
    approved_providers: list[dict],
    cost_commitments: list[dict],
    unresolved_risks: list[str],
    scope: dict[str, list[str]],
    replaces_lock_version: int | None = None,
    architecture_doc_ref: str | None = None,
    architecture_doc_file_hash: str | None = None,
    follows_lock_request: str | None = None,
    requirements_approval_id: str | None = None,
) -> dict[str, Any]:
    """
    Assemble the canonical lock payload.

    architecture_doc_hash is the hash of the document's normalised text.
    architecture_doc_ref says where it came from (a path, or "inline"),
    and architecture_doc_file_hash is the SHA-256 of the file's bytes —
    the figure a person can check with their own tools. Both are part of
    the payload so the human approves a named document, not a bare hash.

    replaces_lock_version is the version of the active lock this payload
    would supersede (None for a first lock). Hashing it in ties an
    approval to one point in the lock history. Without it, an approval
    given for v1's content could be replayed after v2 to write v3, and a
    request left pending against v1 could be approved after v2 and
    silently drop what v2 added.

    follows_lock_request does the same for the REQUEST history. It is the
    id of the lock request that was the latest one when this payload's
    request was first made (None for a project's first). Approve request
    A; the human changes their mind, and B is requested and approved.
    A's approval is still on record, and without this A's content could
    be asked for again and written on that old approval. With it, asking
    for A's content after B is a different payload, so a new request,
    so a new approval.

    requirements_approval_id is the human confirmation of the scope that
    was in force when the lock was requested. The requirements hash alone
    does not say that: confirm scope A, have a lock approved, confirm B,
    go back to A and confirm it again. The hash is A's throughout, but the
    lock approval was given before the human changed the scope twice. With
    the confirmation named in the payload, that approval no longer matches
    and a lock for the re-confirmed scope is asked for again.

    Each decision: {need_id, need_slug, decision_record_id, verdict,
    capability_version_id, capability_key, pinned_revision,
    pinned_source_asset, decided_by, coverage_outcome, flags[],
    license, license_source, cost_tier, satisfies[], search_terms[],
    computed_verdict, computed_capability_key, rationale}.
    """
    return {
        "schema": PAYLOAD_SCHEMA,
        "project_id": project_id,
        "replaces_lock_version": replaces_lock_version,
        "follows_lock_request": follows_lock_request,
        "requirements_approval_id": requirements_approval_id,
        "requirements_hash": requirements_hash,
        "architecture_doc_hash": architecture_doc_hash,
        "architecture_doc_ref": architecture_doc_ref if architecture_doc_hash else None,
        "architecture_doc_file_hash": architecture_doc_file_hash if architecture_doc_hash else None,
        "decisions": sorted(
            ({**d, "flags": sorted(d.get("flags") or []),
              "satisfies": sorted(d.get("satisfies") or [])} for d in decisions),
            key=lambda d: d["need_slug"],
        ),
        "project_decisions": project_decisions,
        "approved_providers": approved_providers,
        "cost_commitments": cost_commitments,
        "unresolved_risks": unresolved_risks,
        "scope": {k: sorted(v) for k, v in sorted(scope.items())},
    }


def payload_hash(payload: dict[str, Any]) -> str:
    return content_hash(payload)


def content_core_hash(payload: dict[str, Any]) -> str:
    """
    Hash of what the lock says, leaving out where it sits in history
    (which version it replaces, which request it follows, which
    confirmation of the requirements it was made under). Two payloads
    with the same core hash freeze the same thing; used to recognise
    "nothing has changed since the active lock" and "this is the request
    already open".
    """
    return content_hash({k: v for k, v in payload.items() if k not in _HISTORY_KEYS})


_HISTORY_KEYS = ("replaces_lock_version", "follows_lock_request", "requirements_approval_id")


def decisions_hash(payload: dict[str, Any]) -> str:
    """Hash of the decisions list alone — what v1.0 called source_lock_hash."""
    return content_hash(payload["decisions"])


# ---------------------------------------------------------------------------
# Flags — what the human must look at
# ---------------------------------------------------------------------------

def lock_flags(payload: dict[str, Any]) -> list[dict[str, str]]:
    """
    Deterministic list of items needing human attention, most serious
    first. Each: {code, subject, detail}.
    """
    flags: list[dict[str, str]] = []

    def add(code: str, subject: str, detail: str) -> None:
        flags.append({"code": code, "subject": subject, "detail": detail})

    for d in payload["decisions"]:
        slug = d["need_slug"]
        verdict = d["verdict"]
        cap = d.get("capability_key") or "?"

        if d["decided_by"] == "llm_override":
            add("llm_override", slug,
                f"{verdict} overrides the verdict CIP computed for this need")
        if FLAG_BLOCKING_GAP in d["flags"]:
            add("overrides_blocking_gap", slug,
                f"{verdict} of {cap} despite a blocking fit gap (license or forbidden dependency)")
        if FLAG_UNANALYSED in d["flags"]:
            add("unanalysed_candidate", slug,
                f"CIP lacks analysed evidence for {cap} on part of what this need's "
                f"constraints check; that part was not verified from source")
        if FLAG_UNEVALUABLE in d["flags"]:
            add("unevaluable_candidates", slug,
                "BUILD although the registry returned matches CIP could not evaluate "
                "(no source to pin, or excluded by this project's policy)")
        if FLAG_OTHER_ACCEPTED in d["flags"]:
            add("build_over_acceptable_candidate", slug,
                "BUILD although CIP's rules would accept a candidate that was evaluated")
        if FLAG_TRUNCATED in d["flags"]:
            add("sweep_truncated", slug,
                "BUILD although the search reached its ceiling; there are matches "
                "CIP never evaluated")
        if FLAG_BANKED_MATCH in d["flags"]:
            add("build_despite_banked_match", slug,
                "BUILD although the evaluation store holds a matching banked asset")
        if FLAG_BANKED_CHANGED in d["flags"]:
            add("banked_evaluation_changed", slug,
                "BUILD; a banked evaluation that matched this need was rewritten during "
                "this project and no longer matches (audit trail: evaluation_replaced)")
        if FLAG_COLD_START in d["flags"]:
            searched = ", ".join(f'"{x}"' for x in d.get("search_terms") or []) \
                or "no terms recorded"
            add("cold_start_build", slug,
                f"BUILD with no registry coverage: CIP had nothing to compare against "
                f"(searched: {searched})")
        if d["decided_by"] == "llm_judgment":
            if WHY_INSUFFICIENT in d["flags"]:
                why = "CIP lacked the evidence to compute a verdict for the candidates"
            elif WHY_TRUNCATED in d["flags"]:
                why = "the search did not see every match"
            elif WHY_OTHER_ACCEPTED in d["flags"]:
                why = ("CIP's rules reject the best-ranked candidate and would accept "
                       "another, so it could not say that nothing fits")
            elif WHY_NO_CONSTRAINT in d["flags"]:
                why = "the need has no machine-checkable constraints"
            else:
                why = "CIP did not compute a verdict for this need"
            add("llm_judgment", slug, f"{verdict} is LLM judgment; {why}")

        if verdict in REUSE_VERDICTS:
            lic = (d.get("license") or "").strip()
            src = {"evidence": "read from its source",
                   "metadata": "registry metadata, not read from source"}.get(
                       d.get("license_source") or "", "source not recorded")
            if not lic:
                if verdict in DEPENDENCY_VERDICTS:
                    add("unknown_license", slug, f"{cap} has no identified license")
            elif any(part.strip().upper() not in PERMISSIVE_LICENSES
                     for part in lic.split(",")):
                add("non_permissive_license", slug, f"{cap} is licensed {lic} ({src})")
            tier = (d.get("cost_tier") or "").strip()
            if tier and tier not in FREE_COST_TIERS:
                add("paid_capability", slug, f"{cap} has cost tier '{tier}'")

    for p in payload["approved_providers"]:
        if p["category"] in SENSITIVE_PROVIDER_CATEGORIES:
            add("sensitive_provider", p["name"],
                f"external {p['category']} provider")
        else:
            known = known_provider_category(p["name"])
            if known:
                add("sensitive_provider", p["name"],
                    f"declared as '{p['category']}', but this is a known {known} provider")
            elif non_ascii_letters(p["name"]):
                # The known-provider list is ASCII names. "ОКТА" written in
                # Cyrillic reads as Okta and matches nothing on it. CIP
                # cannot tell a look-alike from a provider whose name
                # really is in another alphabet, so it says so and the
                # person reading decides.
                add("sensitive_provider", p["name"],
                    f"declared as '{p['category']}'; the name is not written in plain "
                    f"ASCII letters, so it could not be checked against the list of "
                    f"known ai, auth and payment providers. Check it is not a "
                    f"look-alike of one")
        if p["cost"] not in FREE_COST_TIERS:
            add("paid_provider", p["name"], f"cost: {p['cost']}")
        if p["data_leaves_infra"]:
            add("data_leaves_infra", p["name"],
                "receives project or user data outside controlled infrastructure")

    for c in payload["cost_commitments"]:
        # The kind is the agent's label. An amount above zero is a cost
        # whatever it was filed under.
        if c["kind"] not in FREE_COST_TIERS or _positive(c["amount"]):
            amount = " ".join(x for x in (c["amount"], c["currency"]) if x)
            period = f" per {c['period']}" if c["period"] else ""
            add("cost_commitment", c["item"], f"{c['kind']} {amount}{period}".strip())

    for r in payload["unresolved_risks"]:
        add("unresolved_risk", "risk", r)

    if not payload.get("architecture_doc_hash"):
        add("no_architecture_doc", "architecture",
            "no architecture document was supplied; the lock covers decisions only")

    bespoke = payload["scope"].get("bespoke") or []
    if bespoke:
        add("bespoke_requirements", f"{len(bespoke)} requirement(s)",
            "built without a reuse search: " + ", ".join(bespoke))

    order = {code: i for i, code in enumerate(_FLAG_ORDER)}
    return sorted(flags, key=lambda f: (order.get(f["code"], 99), f["subject"], f["detail"]))


_FLAG_ORDER = (
    "overrides_blocking_gap", "llm_override", "non_permissive_license",
    "unknown_license", "unanalysed_candidate", "sensitive_provider", "data_leaves_infra",
    "paid_provider", "paid_capability", "cost_commitment",
    "build_over_acceptable_candidate",
    "build_despite_banked_match", "banked_evaluation_changed",
    "unevaluable_candidates", "sweep_truncated",
    "cold_start_build", "llm_judgment",
    "unresolved_risk", "bespoke_requirements", "no_architecture_doc",
)


def known_provider_category(name: str) -> str | None:
    """The sensitive category of a well-known provider, or None."""
    folded = fold_name(name)
    for category, names in KNOWN_PROVIDERS.items():
        if any(fold_name(n) in folded for n in names):
            return category
    return None


def _positive(amount: Any) -> bool:
    """
    True if `amount` holds any digit other than zero. Deliberately not a
    parse: '2,400', '1.000.000', '$5', '1e3' and '٥٠٠' are all money, and
    a format this function failed to read would otherwise count as free.
    """
    return any(unicodedata.digit(ch, 0) > 0 for ch in str(amount or ""))


# ---------------------------------------------------------------------------
# Diff against the active lock — the change request
# ---------------------------------------------------------------------------

def diff_payloads(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """
    What changes if `new` replaces `old`. A relock approval shows this,
    which is what makes it a change request rather than a blind re-sign.

    The diff is only a change request if it is complete. Every part of
    the payload is compared and described in full, and if the two differ
    in some way none of the comparisons itemised, a catch-all line says
    so. It never reports "no changes" for content that changed.
    """
    if old is None:
        return {"is_first_lock": True, "changes": []}
    try:
        return _diff_payloads(old, new)
    except (TypeError, KeyError, AttributeError, ValueError):
        # The active lock's stored content is not in a shape that can be
        # compared item by item (a row this version did not write). Say so:
        # the one thing this function must not do is report nothing.
        return {"is_first_lock": False, "changes": [{
            "kind": "other_change", "subject": "lock",
            "detail": "the active lock's stored content could not be compared item by "
                      "item with this one; read the whole summary",
        }]}


def _diff_payloads(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, str]] = []

    def add(kind: str, subject: str, detail: str) -> None:
        changes.append({"kind": kind, "subject": subject, "detail": detail})

    def keyed(items: list[dict], key: str) -> dict[str, dict]:
        return {str(i[key]).lower(): i for i in items if isinstance(i, dict) and key in i}

    def compare(label: str, old_items: dict, new_items: dict, describe) -> None:
        for k in sorted(set(old_items) | set(new_items)):
            o, n = old_items.get(k), new_items.get(k)
            if o is None:
                add(f"{label}_added", k, describe(n))
            elif n is None:
                add(f"{label}_removed", k, describe(o))
            elif canonical_json(o) != canonical_json(n):
                before, after = describe(o), describe(n)
                if before == after:
                    # The two differ in something the description does not
                    # show (an internal id, the capitalisation of a name).
                    # Name it, so the line never reads "X -> X".
                    fields = sorted(f for f in set(o) | set(n)
                                    if canonical_json(o.get(f)) != canonical_json(n.get(f)))
                    shown = "; ".join(
                        f"{f}: {one_line(o.get(f), 60)} -> {one_line(n.get(f), 60)}"
                        for f in fields)
                    after = f"{after} [{shown}]"
                add(f"{label}_changed", k, f"{before}  ->  {after}")

    def decision(d: dict) -> str:
        # Everything _decision_core compares is rendered, so a change can
        # never read "X -> X".
        target = d.get("capability_key") or "no capability"
        rev = f" @{d['pinned_revision']}" if d.get("pinned_revision") else ""
        asset = f" from {d['pinned_source_asset']}" if d.get("pinned_source_asset") else ""
        extras = [str(d.get("decided_by"))]
        if d.get("flags"):
            extras.append(", ".join(d["flags"]))
        if d.get("capability_key"):
            extras.append(f"license {d.get('license') or 'none'} ({d.get('license_source') or '?'})")
            extras.append(f"cost tier {d.get('cost_tier') or 'none'}")
        extras.append(f"registry {d.get('coverage_outcome')}")
        return f"{d['verdict']} {target}{rev}{asset} ({'; '.join(extras)})"

    def provider(p: dict) -> str:
        data = "data leaves infrastructure" if p.get("data_leaves_infra") else "no data leaves"
        purpose = f", for: {p['purpose']}" if p.get("purpose") else ""
        return f"{p.get('category')}, {p.get('cost')}, {data}{purpose}"

    def cost(c: dict) -> str:
        amount = " ".join(x for x in (c.get("amount"), c.get("currency")) if x)
        period = f" per {c['period']}" if c.get("period") else ""
        return f"{c.get('kind')} {amount}{period}".strip()

    def project_decision(d: dict) -> str:
        why = f" - {d['rationale']}" if d.get("rationale") else ""
        return f"{d.get('choice')} [{d.get('category')}]{why}"

    old_core = keyed(_decision_core(old.get("decisions") or []), "need_slug")
    new_core = keyed(_decision_core(new["decisions"]), "need_slug")
    compare("decision", old_core, new_core, decision)
    # A decision whose outcome reads the same can still differ: in what it
    # serves, in what was searched, or in which decision record the lock
    # points at. Each decision is compared whole, so nothing about one can
    # change without a line here.
    old_full = keyed([d for d in old.get("decisions") or [] if isinstance(d, dict)], "need_slug")
    for d in new["decisions"]:
        k = str(d["need_slug"]).lower()
        o = old_full.get(k)
        if (o is None or k not in old_core or k not in new_core
                or canonical_json(old_core[k]) != canonical_json(new_core[k])):
            continue                              # added, or already itemised above
        parts = []
        for label, field in (("serves", "satisfies"), ("searched for", "search_terms")):
            before, after = sorted(o.get(field) or []), sorted(d.get(field) or [])
            if before != after:
                parts.append(f"{label}: {', '.join(before) or 'nothing'}"
                             f"  ->  {', '.join(after) or 'nothing'}")
        named = set(_DECISION_CORE_FIELDS) | {"satisfies", "search_terms", "decision_record_id"}
        other = sorted(f for f in set(o) | set(d)
                       if f not in named and canonical_json(o.get(f)) != canonical_json(d.get(f)))
        if other:
            parts.append("also changed: " + ", ".join(other))
        if parts:
            add("decision_changed", k, "; ".join(parts))
        elif o.get("decision_record_id") != d.get("decision_record_id"):
            add("decision_rerecorded", k, "decided again, with the same outcome")
    compare("project_decision", keyed(old.get("project_decisions") or [], "key"),
            keyed(new["project_decisions"], "key"), project_decision)
    compare("provider", keyed(old.get("approved_providers") or [], "name"),
            keyed(new["approved_providers"], "name"), provider)
    compare("cost", keyed(old.get("cost_commitments") or [], "item"),
            keyed(new["cost_commitments"], "item"), cost)

    old_risks, new_risks = set(old.get("unresolved_risks") or []), set(new["unresolved_risks"])
    for r in sorted(old_risks - new_risks):
        add("risk_removed", "risk", r)
    for r in sorted(new_risks - old_risks):
        add("risk_added", "risk", r)

    labels = {"in_scope": "in scope", "bespoke": "bespoke", "deferred": "deferred",
              "not_applicable": "not applicable"}
    old_scope, new_scope = old.get("scope") or {}, new.get("scope") or {}
    for key in sorted(set(old_scope) | set(new_scope)):
        before, after = set(old_scope.get(key) or []), set(new_scope.get(key) or [])
        if before - after:
            add("scope_changed", labels.get(key, key),
                "no longer: " + ", ".join(sorted(before - after)))
        if after - before:
            add("scope_changed", labels.get(key, key),
                "now: " + ", ".join(sorted(after - before)))

    if old.get("requirements_hash") != new["requirements_hash"]:
        add("requirements_changed", "requirements",
            "the confirmed requirements differ from the locked ones")
    if old.get("architecture_doc_hash") != new.get("architecture_doc_hash"):
        add("architecture_doc_changed", "architecture",
            "the architecture document's content differs from the locked one")
    elif (old.get("architecture_doc_ref") != new.get("architecture_doc_ref")
          or old.get("architecture_doc_file_hash") != new.get("architecture_doc_file_hash")):
        add("architecture_doc_changed", "architecture",
            f"same content, read from a different place: {old.get('architecture_doc_ref')}"
            f"  ->  {new.get('architecture_doc_ref')}")

    # Whatever is left: a part of the payload none of the comparisons above
    # reads (the schema, or a field added later and not yet itemised).
    def rest(p: dict) -> dict:
        return {k: v for k, v in p.items() if k not in _DIFFED_KEYS}

    if canonical_json(rest(old)) != canonical_json(rest(new)) or (
            not changes and content_core_hash(old) != content_core_hash(new)):
        add("other_change", "lock",
            "the content differs from the active lock in a way not itemised here; "
            "read the whole summary")
    return {"is_first_lock": False, "changes": changes}


# Top-level payload keys diff_payloads compares one by one.
_DIFFED_KEYS = frozenset((
    "replaces_lock_version", "follows_lock_request", "requirements_approval_id",
    "requirements_hash", "architecture_doc_hash",
    "architecture_doc_ref", "architecture_doc_file_hash", "decisions",
    "project_decisions", "approved_providers", "cost_commitments",
    "unresolved_risks", "scope",
))
_DECISION_CORE_FIELDS = (
    "need_slug", "verdict", "capability_version_id", "capability_key",
    "pinned_revision", "pinned_source_asset", "decided_by", "flags",
    "license", "license_source", "cost_tier", "coverage_outcome",
)


def _decision_core(decisions: list[dict]) -> list[dict]:
    """
    What a decision says, without the ids that say which row said it.
    Who decided is included: the same ADOPT moving from CIP's rules to an
    LLM override is something the human should see change.
    """
    return [
        {f: (sorted(d.get("flags") or []) if f == "flags" else d.get(f))
         for f in _DECISION_CORE_FIELDS}
        for d in decisions
        if isinstance(d, dict) and d.get("need_slug") is not None
    ]


# ---------------------------------------------------------------------------
# Summary — what the human reads before approving
# ---------------------------------------------------------------------------

def render_lock_summary(
    *,
    project_name: str,
    next_version: int,
    payload: dict[str, Any],
    flags: list[dict[str, str]],
    diff: dict[str, Any],
) -> str:
    """
    What the human reads before approving.

    Two rules. Everything in the payload is shown, because a field that is
    hashed but not rendered would be approved unseen. And every value is
    passed through one_line(): the text comes from the agent whose work is
    being approved, and it is printed in a terminal, so a control sequence
    or a newline in it must not be able to redraw or forge the summary.
    ASCII punctuation only, so it survives a cp1252 console or pipe.
    """
    t = one_line
    lines = [f"# Architecture lock v{next_version} - {t(project_name)}", ""]

    if not diff.get("is_first_lock"):
        lines.append(f"## Changes from lock v{next_version - 1}")
        if diff["changes"]:
            for c in diff["changes"]:
                lines.append(
                    f"- {t(c['kind']).replace('_', ' ')}: {t(c['subject'])} - {t(c['detail'])}"
                )
        else:
            lines.append("- none: the content is identical to the active lock")
        lines.append("")

    lines.append("## Needs your attention")
    if flags:
        for f in flags:
            lines.append(f"- [{t(f['code'])}] {t(f['subject'])} - {t(f['detail'])}")
    else:
        lines.append("- nothing flagged")
    lines.append("")

    decs = payload["decisions"]
    depend = [d for d in decs if d["verdict"] in DEPENDENCY_VERDICTS]
    refer = [d for d in decs if d["verdict"] == "REFERENCE"]
    build = [d for d in decs if d["verdict"] == "BUILD"]
    answered = [d for d in decs if d["coverage_outcome"] == "candidates_evaluated"]
    lines.append("## Decisions")
    lines.append(
        f"{len(decs)} capability need(s): {len(depend)} reused as a dependency, "
        f"{len(refer)} built using a reference, {len(build)} built. "
        f"The registry had candidates CIP could evaluate for {len(answered)} of {len(decs)}."
    )
    for d in decs:
        target = f" -> {t(d['capability_key'])}" if d.get("capability_key") else ""
        serves = ", ".join(t(x) for x in d["satisfies"]) or "nothing"
        lines.append(
            f"- {t(d['need_slug'])} [{t(d['verdict'])}]{target} "
            f"({t(d['decided_by'])}; serves {serves})"
        )
        if d.get("capability_key"):
            asset = t(d.get("pinned_source_asset")) or "source not recorded"
            rev = t(d.get("pinned_revision")) or "no revision"
            lic = t(d.get("license")) or "none identified"
            src = {"evidence": "read from source", "metadata": "registry metadata"}.get(
                d.get("license_source") or "", "source not recorded")
            lines.append(f"    pinned: {asset} @{rev}")
            lines.append(
                f"    license: {lic} ({src}); cost tier: {t(d.get('cost_tier')) or 'not recorded'}"
            )
        terms = ", ".join(f'"{t(x)}"' for x in d.get("search_terms") or [])
        lines.append(
            f"    registry: {t(d['coverage_outcome']).replace('_', ' ')}; "
            f"searched for: {terms or 'not recorded'}"
        )
        if d["decided_by"] != "cip_rules":
            computed = t(d.get("computed_verdict"))
            if computed:
                on = f" of {t(d['computed_capability_key'])}" if d.get(
                    "computed_capability_key") else ""
                lines.append(f"    CIP computed: {computed}{on}")
            else:
                lines.append("    CIP computed: nothing it would stand behind")
            lines.append(f"    agent's reason: {t(d.get('rationale')) or 'none recorded'}")
    lines.append("")

    lines.append("## Cross-cutting decisions")
    if payload["project_decisions"]:
        for d in payload["project_decisions"]:
            why = f" - {t(d['rationale'])}" if d["rationale"] else ""
            lines.append(f"- {t(d['key'])} [{t(d['category'])}]: {t(d['choice'])}{why}")
    else:
        lines.append("- none recorded")
    lines.append("")

    lines.append("## External providers")
    if payload["approved_providers"]:
        for p in payload["approved_providers"]:
            data = "data leaves infrastructure" if p["data_leaves_infra"] else "no data leaves"
            purpose = f" - {t(p['purpose'])}" if p["purpose"] else ""
            lines.append(f"- {t(p['name'])} ({t(p['category'])}, {t(p['cost'])}, {data}){purpose}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Cost commitments")
    if payload["cost_commitments"]:
        for c in payload["cost_commitments"]:
            amount = " ".join(x for x in (t(c["amount"]), t(c["currency"])) if x)
            period = f" per {t(c['period'])}" if c["period"] else ""
            lines.append(f"- {t(c['item'])}: {t(c['kind'])} {amount}{period}".rstrip())
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Unresolved risks")
    if payload["unresolved_risks"]:
        for r in payload["unresolved_risks"]:
            lines.append(f"- {t(r)}")
    else:
        lines.append("- none recorded")
    lines.append("")

    scope = payload["scope"]
    lines.append("## Scope")
    for key, label in (("in_scope", "In scope"), ("bespoke", "Bespoke (no reuse search)"),
                       ("deferred", "Deferred"), ("not_applicable", "Not applicable")):
        items = [t(x) for x in scope.get(key) or []]
        lines.append(f"- {label}: {len(items)}" + (f" - {', '.join(items)}" if items else ""))
    lines.append("")
    lines.append("## Architecture document")
    arch = payload.get("architecture_doc_hash")
    if not arch:
        lines.append("- none supplied")
    else:
        ref = payload.get("architecture_doc_ref")
        if ref == "inline":
            lines.append("- TEXT SUPPLIED INLINE BY THE AGENT, not read from a file")
        elif ref:
            lines.append(f"- File: {t(ref)}")
            if payload.get("architecture_doc_file_hash"):
                lines.append(
                    f"- SHA-256 of that file when read: {t(payload['architecture_doc_file_hash'])}"
                )
        else:
            lines.append("- source not recorded")
        lines.append(f"- Content hash (line endings normalised): {t(arch)[:16]}")
    lines.append("")
    lines.append(f"Requirements hash: {payload['requirements_hash'][:16]}")
    replaces = payload.get("replaces_lock_version")
    lines.append(
        f"Replaces: lock v{replaces}" if replaces else "Replaces: nothing (first lock)"
    )
    # Where this request sits in history. Both are part of what is hashed,
    # so both are shown: an approval is for this content at this point.
    confirmed = payload.get("requirements_approval_id")
    lines.append(
        f"Made under requirements confirmation: {t(confirmed)[:8]}" if confirmed
        else "Made under requirements confirmation: not recorded"
    )
    follows = payload.get("follows_lock_request")
    lines.append(
        f"Follows lock request: {t(follows)[:8]}" if follows
        else "Follows lock request: none (the project's first)"
    )
    return "\n".join(lines)


def _s(v: Any) -> str:
    """A clean NFC string, or "" when nothing in it would be visible."""
    if not isinstance(v, str) or is_blank(v):
        return ""
    return nfc(v).strip()
