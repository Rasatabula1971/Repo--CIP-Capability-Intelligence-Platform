"""
License classification engine — Phase 1.

Deterministic classifier that maps SPDX license identifiers to a
license_status used by the lifecycle and recommendation pipeline.

Four statuses:
  verified_open_source — permissive or weak-copyleft; safe to reuse
  needs_review         — copyleft, dual-license, or uncommon; human decides
  blocked              — no license, source-available, or explicitly denied
  unknown              — license data not yet evaluated

The classifier never calls an LLM. It uses a categorized SPDX table and
an optional project-level license_policy_profile to override defaults.

Usage:
    from core.policy.license import classify_license, LicenseStatus

    result = classify_license("MIT")
    assert result.status == LicenseStatus.VERIFIED_OPEN_SOURCE
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LicenseStatus(str, Enum):
    VERIFIED_OPEN_SOURCE = "verified_open_source"
    NEEDS_REVIEW         = "needs_review"
    BLOCKED              = "blocked"
    UNKNOWN              = "unknown"


class LicenseCategory(str, Enum):
    PERMISSIVE     = "permissive"
    WEAK_COPYLEFT  = "weak_copyleft"
    STRONG_COPYLEFT = "strong_copyleft"
    NETWORK_COPYLEFT = "network_copyleft"
    PUBLIC_DOMAIN  = "public_domain"
    NON_FREE       = "non_free"
    UNKNOWN        = "unknown"


@dataclass(frozen=True)
class LicenseClassification:
    spdx_id: str | None
    status: LicenseStatus
    category: LicenseCategory
    reason: str
    obligations: list[str]


# -----------------------------------------------------------------------
# SPDX → category mapping
# -----------------------------------------------------------------------

_PERMISSIVE: frozenset[str] = frozenset({
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "Unlicense",
    "0BSD",
    "CC0-1.0",
    "Zlib",
    "BSL-1.0",
    "MIT-0",
    "PSF-2.0",
    "WTFPL",
    "X11",
    "curl",
    "PostgreSQL",
    "BlueOak-1.0.0",
})

_WEAK_COPYLEFT: frozenset[str] = frozenset({
    "MPL-2.0",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
    "LGPL-2.1",
    "LGPL-3.0",
    "EPL-1.0",
    "EPL-2.0",
    "CDDL-1.0",
    "CPL-1.0",
    "OSL-3.0",
    "EUPL-1.2",
    "Artistic-2.0",
})

_STRONG_COPYLEFT: frozenset[str] = frozenset({
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "GPL-2.0",
    "GPL-3.0",
})

_NETWORK_COPYLEFT: frozenset[str] = frozenset({
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
    "AGPL-3.0",
    "SSPL-1.0",
})

_NON_FREE: frozenset[str] = frozenset({
    "BSL-1.1",
    "Elastic-2.0",
    "Commons-Clause",
})

_PUBLIC_DOMAIN: frozenset[str] = frozenset({
    "Unlicense",
    "CC0-1.0",
    "0BSD",
})

# Obligations by category
_OBLIGATIONS: dict[LicenseCategory, list[str]] = {
    LicenseCategory.PERMISSIVE: [
        "include_license_notice",
        "include_copyright_notice",
    ],
    LicenseCategory.WEAK_COPYLEFT: [
        "include_license_notice",
        "include_copyright_notice",
        "disclose_modifications_to_licensed_files",
        "license_derivative_under_same_terms",
    ],
    LicenseCategory.STRONG_COPYLEFT: [
        "include_license_notice",
        "include_copyright_notice",
        "disclose_source_of_derivative_works",
        "license_derivative_under_same_terms",
    ],
    LicenseCategory.NETWORK_COPYLEFT: [
        "include_license_notice",
        "include_copyright_notice",
        "disclose_source_of_derivative_works",
        "license_derivative_under_same_terms",
        "provide_source_to_network_users",
    ],
    LicenseCategory.PUBLIC_DOMAIN: [],
    LicenseCategory.NON_FREE: ["cannot_reuse"],
    LicenseCategory.UNKNOWN: ["requires_manual_review"],
}


def _categorize(spdx_id: str) -> LicenseCategory:
    if spdx_id in _PERMISSIVE or spdx_id in _PUBLIC_DOMAIN:
        return LicenseCategory.PERMISSIVE
    if spdx_id in _WEAK_COPYLEFT:
        return LicenseCategory.WEAK_COPYLEFT
    if spdx_id in _STRONG_COPYLEFT:
        return LicenseCategory.STRONG_COPYLEFT
    if spdx_id in _NETWORK_COPYLEFT:
        return LicenseCategory.NETWORK_COPYLEFT
    if spdx_id in _NON_FREE:
        return LicenseCategory.NON_FREE
    return LicenseCategory.UNKNOWN


def _default_status(category: LicenseCategory) -> LicenseStatus:
    if category == LicenseCategory.PERMISSIVE:
        return LicenseStatus.VERIFIED_OPEN_SOURCE
    if category == LicenseCategory.WEAK_COPYLEFT:
        return LicenseStatus.VERIFIED_OPEN_SOURCE
    if category in (LicenseCategory.STRONG_COPYLEFT,
                    LicenseCategory.NETWORK_COPYLEFT):
        return LicenseStatus.NEEDS_REVIEW
    if category == LicenseCategory.NON_FREE:
        return LicenseStatus.BLOCKED
    return LicenseStatus.NEEDS_REVIEW


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------

def classify_license(
    spdx_id: str | None,
    *,
    allowed_spdx: frozenset[str] | None = None,
    denied_spdx: frozenset[str] | None = None,
) -> LicenseClassification:
    """
    Classify a single SPDX license identifier.

    Args:
        spdx_id: The SPDX identifier, or None / "unknown" for missing.
        allowed_spdx: Project-level override allowlist. If set, only
            these licenses are verified_open_source; everything else
            that would normally pass is needs_review instead.
        denied_spdx: Project-level denylist. Any license in this set
            is blocked regardless of category.

    Returns a LicenseClassification with status, category, reason,
    and obligations.
    """
    if not spdx_id or spdx_id == "unknown":
        return LicenseClassification(
            spdx_id=spdx_id,
            status=LicenseStatus.BLOCKED,
            category=LicenseCategory.UNKNOWN,
            reason="no license detected; no-license means no reuse",
            obligations=["cannot_reuse"],
        )

    # Project denylist overrides everything.
    if denied_spdx and spdx_id in denied_spdx:
        category = _categorize(spdx_id)
        return LicenseClassification(
            spdx_id=spdx_id,
            status=LicenseStatus.BLOCKED,
            category=category,
            reason=f"{spdx_id} is on the project denylist",
            obligations=["cannot_reuse"],
        )

    category = _categorize(spdx_id)
    status = _default_status(category)
    obligations = list(_OBLIGATIONS.get(category, []))

    # Project allowlist narrows what counts as verified.
    if allowed_spdx and status == LicenseStatus.VERIFIED_OPEN_SOURCE:
        if spdx_id not in allowed_spdx:
            status = LicenseStatus.NEEDS_REVIEW
            reason = (
                f"{spdx_id} ({category.value}) is open source but "
                f"not on the project allowlist"
            )
            return LicenseClassification(
                spdx_id=spdx_id,
                status=status,
                category=category,
                reason=reason,
                obligations=obligations,
            )

    reasons = {
        LicenseStatus.VERIFIED_OPEN_SOURCE:
            f"{spdx_id} is {category.value}; safe to reuse",
        LicenseStatus.NEEDS_REVIEW:
            f"{spdx_id} is {category.value}; requires human review",
        LicenseStatus.BLOCKED:
            f"{spdx_id} is {category.value}; cannot be reused",
    }
    reason = reasons.get(status, f"{spdx_id}: {category.value}")

    return LicenseClassification(
        spdx_id=spdx_id,
        status=status,
        category=category,
        reason=reason,
        obligations=obligations,
    )


def classify_multi(
    spdx_ids: list[str],
    *,
    allowed_spdx: frozenset[str] | None = None,
    denied_spdx: frozenset[str] | None = None,
) -> LicenseClassification:
    """
    Classify multiple licenses (dual/multi-license).

    For dual-license, the most permissive one wins — the user can
    choose which license to comply with.
    """
    if not spdx_ids:
        return classify_license(
            None, allowed_spdx=allowed_spdx, denied_spdx=denied_spdx
        )
    if len(spdx_ids) == 1:
        return classify_license(
            spdx_ids[0],
            allowed_spdx=allowed_spdx,
            denied_spdx=denied_spdx,
        )

    classifications = [
        classify_license(
            sid, allowed_spdx=allowed_spdx, denied_spdx=denied_spdx
        )
        for sid in spdx_ids
    ]

    _STATUS_RANK = {
        LicenseStatus.VERIFIED_OPEN_SOURCE: 0,
        LicenseStatus.NEEDS_REVIEW: 1,
        LicenseStatus.BLOCKED: 2,
        LicenseStatus.UNKNOWN: 3,
    }

    best = min(classifications, key=lambda c: _STATUS_RANK[c.status])

    if best.status == LicenseStatus.VERIFIED_OPEN_SOURCE:
        return LicenseClassification(
            spdx_id=" OR ".join(spdx_ids),
            status=LicenseStatus.VERIFIED_OPEN_SOURCE,
            category=best.category,
            reason=(
                f"dual-licensed; choosing {best.spdx_id} "
                f"({best.category.value})"
            ),
            obligations=best.obligations,
        )

    return LicenseClassification(
        spdx_id=" OR ".join(spdx_ids),
        status=LicenseStatus.NEEDS_REVIEW,
        category=best.category,
        reason=(
            f"multi-license ({', '.join(spdx_ids)}); "
            f"best option is {best.spdx_id} ({best.status.value})"
        ),
        obligations=best.obligations,
    )


def check_license(
    conn,
    *,
    capability_id,
    project_id=None,
) -> LicenseClassification:
    """
    Check a capability's license against the global policy or a
    project-specific license_policy_profile.

    Reads capability.license_spdx and optionally the project's
    license_policy_profile to produce a classification.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT license_spdx FROM capability WHERE id = %s",
            (str(capability_id),),
        )
        row = cur.fetchone()
        if not row:
            return classify_license(None)
        spdx = row[0]

    allowed = None
    denied = None
    if project_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allowed_spdx, denied_spdx "
                "FROM license_policy_profile "
                "WHERE project_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (str(project_id),),
            )
            policy = cur.fetchone()
            if policy:
                allowed = frozenset(policy[0]) if policy[0] else None
                denied = frozenset(policy[1]) if policy[1] else None

    return classify_license(spdx, allowed_spdx=allowed, denied_spdx=denied)
