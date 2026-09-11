"""
Phase 1 — License enforcement tests.

Coverage:
  1. License classifier:
     - Permissive licenses (MIT, Apache-2.0, BSD, ISC)
     - Weak copyleft (MPL-2.0, LGPL)
     - Strong copyleft (GPL-2.0, GPL-3.0)
     - Network copyleft (AGPL-3.0)
     - Non-free / source-available (BSL-1.1)
     - No license / unknown
     - Dual-license (most permissive wins)
     - Project allowlist narrows acceptance
     - Project denylist blocks
  2. Lifecycle transitions:
     - CANDIDATE -> LICENSE_CHECKED requires non-blocked license
     - CANDIDATE -> LICENSE_CHECKED blocked when license unknown
     - CANDIDATE -> LICENSE_CHECKED blocked when license is blocked
     - LICENSE_CHECKED -> ANALYZED works
     - Old path CANDIDATE -> ANALYZED no longer works
     - block_from_reuse lateral transition
     - mark_needs_review lateral transition
  3. License check query:
     - Returns classification for a capability
"""
from __future__ import annotations

import uuid

import pytest

from core.policy.license import (
    LicenseCategory,
    LicenseClassification,
    LicenseStatus,
    classify_license,
    classify_multi,
)
from core.capability.lifecycle import (
    LifecycleState,
    IllegalTransition,
    GuardsFailed,
    advance_to,
    block_from_reuse,
    current_state,
    mark_needs_review,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# License classifier — pure function tests (no DB needed)
# ---------------------------------------------------------------------------

class TestClassifyLicense:

    def test_mit_is_permissive(self):
        r = classify_license("MIT")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert r.category == LicenseCategory.PERMISSIVE

    def test_apache_is_permissive(self):
        r = classify_license("Apache-2.0")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert r.category == LicenseCategory.PERMISSIVE

    def test_bsd3_is_permissive(self):
        r = classify_license("BSD-3-Clause")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert r.category == LicenseCategory.PERMISSIVE

    def test_bsd2_is_permissive(self):
        r = classify_license("BSD-2-Clause")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE

    def test_isc_is_permissive(self):
        r = classify_license("ISC")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE

    def test_unlicense_is_permissive(self):
        r = classify_license("Unlicense")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE

    def test_mpl2_is_weak_copyleft_and_verified(self):
        r = classify_license("MPL-2.0")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert r.category == LicenseCategory.WEAK_COPYLEFT

    def test_lgpl3_is_weak_copyleft_and_verified(self):
        r = classify_license("LGPL-3.0")
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert r.category == LicenseCategory.WEAK_COPYLEFT

    def test_gpl3_needs_review(self):
        r = classify_license("GPL-3.0")
        assert r.status == LicenseStatus.NEEDS_REVIEW
        assert r.category == LicenseCategory.STRONG_COPYLEFT

    def test_gpl2_needs_review(self):
        r = classify_license("GPL-2.0")
        assert r.status == LicenseStatus.NEEDS_REVIEW
        assert r.category == LicenseCategory.STRONG_COPYLEFT

    def test_agpl3_needs_review(self):
        r = classify_license("AGPL-3.0")
        assert r.status == LicenseStatus.NEEDS_REVIEW
        assert r.category == LicenseCategory.NETWORK_COPYLEFT

    def test_bsl11_is_blocked(self):
        r = classify_license("BSL-1.1")
        assert r.status == LicenseStatus.BLOCKED
        assert r.category == LicenseCategory.NON_FREE

    def test_no_license_is_blocked(self):
        r = classify_license(None)
        assert r.status == LicenseStatus.BLOCKED
        assert "no license" in r.reason

    def test_unknown_spdx_is_blocked(self):
        r = classify_license("unknown")
        assert r.status == LicenseStatus.BLOCKED

    def test_unrecognized_spdx_needs_review(self):
        r = classify_license("SomethingCustom-1.0")
        assert r.status == LicenseStatus.NEEDS_REVIEW
        assert r.category == LicenseCategory.UNKNOWN

    def test_obligations_for_permissive(self):
        r = classify_license("MIT")
        assert "include_license_notice" in r.obligations
        assert "include_copyright_notice" in r.obligations

    def test_obligations_for_strong_copyleft(self):
        r = classify_license("GPL-3.0")
        assert "disclose_source_of_derivative_works" in r.obligations
        assert "license_derivative_under_same_terms" in r.obligations

    def test_obligations_for_network_copyleft(self):
        r = classify_license("AGPL-3.0")
        assert "provide_source_to_network_users" in r.obligations

    def test_project_denylist_blocks_normally_permissive(self):
        r = classify_license("MIT", denied_spdx=frozenset({"MIT"}))
        assert r.status == LicenseStatus.BLOCKED
        assert "denylist" in r.reason

    def test_project_allowlist_narrows_acceptance(self):
        r = classify_license(
            "BSD-3-Clause",
            allowed_spdx=frozenset({"MIT", "Apache-2.0"}),
        )
        assert r.status == LicenseStatus.NEEDS_REVIEW
        assert "not on the project allowlist" in r.reason

    def test_project_allowlist_accepts_listed_license(self):
        r = classify_license(
            "MIT",
            allowed_spdx=frozenset({"MIT", "Apache-2.0"}),
        )
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE


class TestClassifyMulti:

    def test_dual_mit_gpl_picks_mit(self):
        r = classify_multi(["MIT", "GPL-3.0"])
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert "MIT" in r.reason

    def test_dual_gpl_agpl_needs_review(self):
        r = classify_multi(["GPL-3.0", "AGPL-3.0"])
        assert r.status == LicenseStatus.NEEDS_REVIEW

    def test_empty_list_is_blocked(self):
        r = classify_multi([])
        assert r.status == LicenseStatus.BLOCKED

    def test_single_item_delegates(self):
        r = classify_multi(["Apache-2.0"])
        assert r.status == LicenseStatus.VERIFIED_OPEN_SOURCE
        assert r.spdx_id == "Apache-2.0"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _mk_provider_asset_revision(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, 'repository') RETURNING id",
            (provider_id, f"a/{uuid.uuid4().hex[:6]}", "repo"),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, uuid.uuid4().hex),
        )
        return cur.fetchone()[0]


def _mk_capability(
    conn,
    normalized_key: str,
    display_name: str,
    license_spdx: str | None = None,
    license_status: str = "unknown",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, license_spdx, license_status) "
            "VALUES (%s, %s, 'pypi', 'library', %s, %s) RETURNING id",
            (normalized_key, display_name, license_spdx, license_status),
        )
        return cur.fetchone()[0]


def _mk_version(
    conn,
    capability_id: uuid.UUID,
    lifecycle_state: str = "candidate",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version, lifecycle_state) "
            "VALUES (%s, %s, 'content-hash', '0.1.0', %s) RETURNING id",
            (capability_id, f"content:{uuid.uuid4().hex[:8]}", lifecycle_state),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Lifecycle transition tests (DB-backed)
# ---------------------------------------------------------------------------

class TestLifecycleTransitions:

    def test_candidate_to_license_checked_passes_with_verified_license(self, conn):
        cap = _mk_capability(conn, "pypi:good-lib", "good-lib",
                             license_spdx="MIT",
                             license_status="verified_open_source")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        outcome = advance_to(
            conn,
            capability_version_id=ver,
            target=LifecycleState.LICENSE_CHECKED,
        )
        assert outcome.new_state == "license_checked"
        assert not outcome.was_noop

    def test_candidate_to_license_checked_passes_with_needs_review(self, conn):
        cap = _mk_capability(conn, "pypi:gpl-lib", "gpl-lib",
                             license_spdx="GPL-3.0",
                             license_status="needs_review")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        outcome = advance_to(
            conn,
            capability_version_id=ver,
            target=LifecycleState.LICENSE_CHECKED,
        )
        assert outcome.new_state == "license_checked"

    def test_candidate_to_license_checked_blocked_when_unknown(self, conn):
        cap = _mk_capability(conn, "pypi:unknown-lic", "unknown-lic",
                             license_status="unknown")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        with pytest.raises(GuardsFailed) as exc_info:
            advance_to(
                conn,
                capability_version_id=ver,
                target=LifecycleState.LICENSE_CHECKED,
            )
        assert any(f.guard_name == "license_checked"
                   for f in exc_info.value.failures)

    def test_candidate_to_license_checked_blocked_when_blocked(self, conn):
        cap = _mk_capability(conn, "pypi:no-license", "no-license",
                             license_status="blocked")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        with pytest.raises(GuardsFailed) as exc_info:
            advance_to(
                conn,
                capability_version_id=ver,
                target=LifecycleState.LICENSE_CHECKED,
            )
        failures = exc_info.value.failures
        assert len(failures) == 1
        assert "blocked" in failures[0].reason

    def test_license_checked_to_analyzed_works(self, conn):
        cap = _mk_capability(conn, "pypi:ok-lib", "ok-lib",
                             license_spdx="Apache-2.0",
                             license_status="verified_open_source")
        ver = _mk_version(conn, cap, lifecycle_state="license_checked")
        conn.commit()

        outcome = advance_to(
            conn,
            capability_version_id=ver,
            target=LifecycleState.ANALYZED,
        )
        assert outcome.new_state == "analyzed"

    def test_candidate_to_analyzed_is_illegal(self, conn):
        """Old direct path CANDIDATE -> ANALYZED no longer works."""
        cap = _mk_capability(conn, "pypi:skip-lic", "skip-lic",
                             license_spdx="MIT",
                             license_status="verified_open_source")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        with pytest.raises(IllegalTransition):
            advance_to(
                conn,
                capability_version_id=ver,
                target=LifecycleState.ANALYZED,
            )


class TestLateralTransitions:

    def test_block_from_reuse_from_candidate(self, conn):
        cap = _mk_capability(conn, "pypi:bad-lic", "bad-lic")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        outcome = block_from_reuse(
            conn,
            capability_version_id=ver,
            reason="no license detected",
        )
        assert outcome.new_state == "blocked_from_reuse"
        assert current_state(conn, ver) == LifecycleState.BLOCKED_FROM_REUSE

    def test_block_from_reuse_is_idempotent(self, conn):
        cap = _mk_capability(conn, "pypi:already-blocked", "already-blocked")
        ver = _mk_version(conn, cap, lifecycle_state="blocked_from_reuse")
        conn.commit()

        outcome = block_from_reuse(
            conn,
            capability_version_id=ver,
            reason="already blocked",
        )
        assert outcome.was_noop

    def test_block_from_reuse_from_cataloged_is_illegal(self, conn):
        cap = _mk_capability(conn, "pypi:cataloged-lib", "cataloged-lib")
        ver = _mk_version(conn, cap, lifecycle_state="cataloged")
        conn.commit()

        with pytest.raises(IllegalTransition):
            block_from_reuse(
                conn,
                capability_version_id=ver,
                reason="too late",
            )

    def test_mark_needs_review_from_candidate(self, conn):
        cap = _mk_capability(conn, "pypi:ambiguous", "ambiguous")
        ver = _mk_version(conn, cap, lifecycle_state="candidate")
        conn.commit()

        outcome = mark_needs_review(
            conn,
            capability_version_id=ver,
            reason="custom license text, not recognizable",
        )
        assert outcome.new_state == "needs_review"
        assert current_state(conn, ver) == LifecycleState.NEEDS_REVIEW

    def test_mark_needs_review_is_idempotent(self, conn):
        cap = _mk_capability(conn, "pypi:pending-review", "pending-review")
        ver = _mk_version(conn, cap, lifecycle_state="needs_review")
        conn.commit()

        outcome = mark_needs_review(
            conn,
            capability_version_id=ver,
            reason="still pending",
        )
        assert outcome.was_noop


# ---------------------------------------------------------------------------
# License check query (DB-backed)
# ---------------------------------------------------------------------------

class TestLicenseCheckQuery:

    def test_returns_classification_for_mit(self, conn):
        cap = _mk_capability(conn, "pypi:test-mit", "test-mit",
                             license_spdx="MIT",
                             license_status="verified_open_source")
        conn.commit()

        result = queries.capability_license_check(conn, str(cap))
        assert result is not None
        assert result["status"] == "verified_open_source"
        assert result["category"] == "permissive"
        assert result["spdx_id"] == "MIT"

    def test_returns_blocked_for_no_license(self, conn):
        cap = _mk_capability(conn, "pypi:test-nolic", "test-nolic",
                             license_spdx=None,
                             license_status="unknown")
        conn.commit()

        result = queries.capability_license_check(conn, str(cap))
        assert result is not None
        assert result["status"] == "blocked"

    def test_returns_none_for_bad_id(self, conn):
        result = queries.capability_license_check(conn, "not-a-uuid")
        assert result is None

    def test_search_results_include_license_status(self, conn):
        _mk_capability(conn, "pypi:lic-test", "lic-test",
                       license_spdx="MIT",
                       license_status="verified_open_source")
        conn.commit()

        rows = queries.search_capabilities(conn, query="lic-test")
        assert len(rows) == 1
        assert rows[0]["license_status"] == "verified_open_source"
