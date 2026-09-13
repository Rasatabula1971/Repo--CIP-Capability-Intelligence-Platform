"""
Phase 4a — Lightweight Verification tests.

Coverage:
  1. Verification runner — pure function tests:
     - AssertionResult creation and fields
     - _truncate truncates long output
     - _compute_reproducibility_hash is deterministic
     - evaluate_assertions_only: all pass → passed
     - evaluate_assertions_only: one fails → failed
     - evaluate_assertions_only: timeout → timeout
     - evaluate_assertions_only: empty → pending
     - VerificationSpec defaults
  2. _run_command — subprocess tests (lightweight):
     - Successful command
     - Failed command (nonzero exit)
     - Command not found
     - Timeout handling
  3. DB-backed verification queries:
     - record_verification_run stores run and assertions
     - verification_status shows latest run
     - verification_status with no runs
     - verification_status with no head version
     - verification_status with bad id returns None
     - record_verification_run with bad id returns None
     - multiple runs ordered by most recent first
"""
from __future__ import annotations

import sys
import uuid

import pytest

from core.verification.runner import (
    AssertionKind,
    AssertionResult,
    RunResult,
    VerificationSpec,
    _compute_reproducibility_hash,
    _run_command,
    _truncate,
    evaluate_assertions_only,
)
from mcp_server import queries


# ---------------------------------------------------------------------------
# Pure function tests — verification runner
# ---------------------------------------------------------------------------

class TestTruncate:

    def test_short_text_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_long_text_truncated(self):
        text = "x" * 200
        result = _truncate(text, 50)
        assert len(result.encode("utf-8")) <= 50 + len("\n[truncated]")
        assert result.endswith("[truncated]")

    def test_empty_text(self):
        assert _truncate("", 100) == ""


class TestReproducibilityHash:

    def test_deterministic(self):
        assertions = [
            AssertionResult(
                assertion_kind="install_success", command="pip install x",
                exit_code=0, stdout="", stderr="", duration_ms=100,
                passed=True, reason="",
            ),
            AssertionResult(
                assertion_kind="import_success", command="python -c 'import x'",
                exit_code=0, stdout="", stderr="", duration_ms=50,
                passed=True, reason="",
            ),
        ]
        h1 = _compute_reproducibility_hash(assertions)
        h2 = _compute_reproducibility_hash(assertions)
        assert h1 == h2
        assert len(h1) == 16

    def test_different_results_different_hash(self):
        a1 = [AssertionResult(
            assertion_kind="install_success", command="pip install x",
            exit_code=0, stdout="", stderr="", duration_ms=100,
            passed=True, reason="",
        )]
        a2 = [AssertionResult(
            assertion_kind="install_success", command="pip install x",
            exit_code=1, stdout="", stderr="", duration_ms=100,
            passed=False, reason="exit code 1",
        )]
        assert _compute_reproducibility_hash(a1) != _compute_reproducibility_hash(a2)


class TestEvaluateAssertionsOnly:

    def test_all_pass(self):
        assertions = [
            {"assertion_kind": "install_success", "passed": True, "exit_code": 0},
            {"assertion_kind": "import_success", "passed": True, "exit_code": 0},
        ]
        result = evaluate_assertions_only(assertions)
        assert result["result"] == "passed"
        assert len(result["reproducibility_hash"]) == 16

    def test_one_fails(self):
        assertions = [
            {"assertion_kind": "install_success", "passed": True, "exit_code": 0},
            {"assertion_kind": "import_success", "passed": False, "exit_code": 1},
        ]
        result = evaluate_assertions_only(assertions)
        assert result["result"] == "failed"

    def test_timeout(self):
        assertions = [
            {"assertion_kind": "install_success", "passed": False,
             "exit_code": None, "reason": "timeout after 120s"},
        ]
        result = evaluate_assertions_only(assertions)
        assert result["result"] == "timeout"

    def test_empty(self):
        result = evaluate_assertions_only([])
        assert result["result"] == "pending"


class TestVerificationSpec:

    def test_defaults(self):
        spec = VerificationSpec(package_name="httpx")
        assert spec.package_name == "httpx"
        assert spec.install_spec is None
        assert spec.import_name is None
        assert spec.build_command is None
        assert spec.smoke_commands == []
        assert spec.contract_commands == []
        assert spec.timeout_s == 120

    def test_custom_values(self):
        spec = VerificationSpec(
            package_name="httpx",
            install_spec="httpx[http2]>=0.24",
            import_name="httpx",
            smoke_commands=["python -c 'import httpx; print(httpx.__version__)'"],
            timeout_s=60,
        )
        assert spec.install_spec == "httpx[http2]>=0.24"
        assert spec.timeout_s == 60


class TestAssertionKind:

    def test_all_kinds_exist(self):
        assert AssertionKind.INSTALL_SUCCESS.value == "install_success"
        assert AssertionKind.BUILD_SUCCESS.value == "build_success"
        assert AssertionKind.IMPORT_SUCCESS.value == "import_success"
        assert AssertionKind.SMOKE_PASS.value == "smoke_pass"
        assert AssertionKind.CONTRACT_PASS.value == "contract_pass"


class TestRunResult:

    def test_all_results_exist(self):
        assert RunResult.PENDING.value == "pending"
        assert RunResult.RUNNING.value == "running"
        assert RunResult.PASSED.value == "passed"
        assert RunResult.FAILED.value == "failed"
        assert RunResult.ERROR.value == "error"
        assert RunResult.TIMEOUT.value == "timeout"


# ---------------------------------------------------------------------------
# _run_command — subprocess tests (lightweight, no venv needed)
# ---------------------------------------------------------------------------

class TestRunCommand:

    def test_successful_command(self):
        result = _run_command(
            [sys.executable, "-c", "print('hello')"],
            timeout_s=10,
        )
        assert result.passed is True
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_failed_command(self):
        result = _run_command(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            timeout_s=10,
        )
        assert result.passed is False
        assert result.exit_code == 42
        assert "42" in result.reason

    def test_command_not_found(self):
        result = _run_command(
            ["nonexistent_command_xyz_12345"],
            timeout_s=5,
        )
        assert result.passed is False
        assert result.exit_code is None
        assert "not found" in result.reason.lower() or "No such file" in result.stderr

    def test_timeout_handling(self):
        result = _run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_s=1,
        )
        assert result.passed is False
        assert "timeout" in result.reason.lower()
        assert result.exit_code is None

    def test_stderr_captured(self):
        result = _run_command(
            [sys.executable, "-c",
             "import sys; sys.stderr.write('err_msg\\n')"],
            timeout_s=10,
        )
        assert "err_msg" in result.stderr


# ---------------------------------------------------------------------------
# Seed helpers (DB tests)
# ---------------------------------------------------------------------------

def _mk_capability(conn, key: str, name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind) "
            "VALUES (%s, %s, 'pypi', 'library') RETURNING id",
            (key, name),
        )
        return cur.fetchone()[0]


def _mk_version(conn, capability_id: uuid.UUID) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version, lifecycle_state) "
            "VALUES (%s, %s, 'content-hash', '2.0.0', 'candidate') RETURNING id",
            (capability_id, f"content:{uuid.uuid4().hex[:8]}"),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# DB-backed verification query tests
# ---------------------------------------------------------------------------

class TestRecordVerificationRun:

    def test_records_run_and_assertions(self, conn):
        cap = _mk_capability(conn, "pypi:verify-a", "verify-a")
        ver = _mk_version(conn, cap)
        conn.commit()

        result = queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="passed",
            environment={"python": "3.11", "platform": "linux"},
            duration_ms=5000,
            logs="install ok\nimport ok",
            reproducibility_hash="abc123",
            triggered_by="test",
            assertions=[
                {
                    "assertion_kind": "install_success",
                    "command": "pip install verify-a",
                    "exit_code": 0,
                    "stdout": "Successfully installed",
                    "stderr": "",
                    "duration_ms": 3000,
                    "passed": True,
                    "reason": "",
                },
                {
                    "assertion_kind": "import_success",
                    "command": "python -c 'import verify_a'",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "duration_ms": 500,
                    "passed": True,
                    "reason": "",
                },
            ],
        )

        assert result is not None
        assert result["result"] == "passed"
        assert result["duration_ms"] == 5000
        assert result["reproducibility_hash"] == "abc123"
        assert len(result["assertions"]) == 2
        assert result["assertions"][0]["assertion_kind"] == "install_success"
        assert result["assertions"][0]["passed"] is True

    def test_bad_version_id_returns_none(self, conn):
        result = queries.record_verification_run(
            conn,
            capability_version_id="not-a-uuid",
            result="passed",
        )
        assert result is None

    def test_failed_run_with_assertions(self, conn):
        cap = _mk_capability(conn, "pypi:verify-b", "verify-b")
        ver = _mk_version(conn, cap)
        conn.commit()

        result = queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="failed",
            duration_ms=2000,
            assertions=[
                {
                    "assertion_kind": "install_success",
                    "command": "pip install verify-b",
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Could not find package",
                    "duration_ms": 2000,
                    "passed": False,
                    "reason": "exit code 1",
                },
            ],
        )

        assert result is not None
        assert result["result"] == "failed"
        assert result["assertions"][0]["passed"] is False


class TestVerificationStatus:

    def test_shows_latest_run(self, conn):
        cap = _mk_capability(conn, "pypi:verify-c", "verify-c")
        ver = _mk_version(conn, cap)
        conn.commit()

        queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="passed",
            duration_ms=5000,
            reproducibility_hash="hash1",
            assertions=[
                {
                    "assertion_kind": "install_success",
                    "command": "pip install verify-c",
                    "exit_code": 0,
                    "passed": True,
                    "reason": "",
                    "duration_ms": 3000,
                },
            ],
        )

        result = queries.verification_status(conn, str(cap))
        assert result is not None
        assert result["verified"] is True
        assert result["normalized_key"] == "pypi:verify-c"
        assert result["display_version"] == "2.0.0"
        assert len(result["runs"]) == 1
        assert result["runs"][0]["result"] == "passed"
        assert len(result["runs"][0]["assertions"]) == 1

    def test_no_runs_means_not_verified(self, conn):
        cap = _mk_capability(conn, "pypi:verify-d", "verify-d")
        _mk_version(conn, cap)
        conn.commit()

        result = queries.verification_status(conn, str(cap))
        assert result is not None
        assert result["verified"] is False
        assert result["runs"] == []

    def test_no_head_version(self, conn):
        cap = _mk_capability(conn, "pypi:verify-e", "verify-e")
        conn.commit()

        result = queries.verification_status(conn, str(cap))
        assert result is not None
        assert result["verified"] is False
        assert "no head version" in result.get("note", "")

    def test_bad_id_returns_none(self, conn):
        result = queries.verification_status(conn, "not-a-uuid")
        assert result is None

    def test_multiple_runs_ordered_by_most_recent(self, conn):
        cap = _mk_capability(conn, "pypi:verify-f", "verify-f")
        ver = _mk_version(conn, cap)
        conn.commit()

        queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="failed",
            duration_ms=1000,
            reproducibility_hash="old_hash",
            assertions=[
                {
                    "assertion_kind": "install_success",
                    "command": "pip install verify-f",
                    "exit_code": 1,
                    "passed": False,
                    "reason": "exit code 1",
                    "duration_ms": 1000,
                },
            ],
        )

        queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="passed",
            duration_ms=3000,
            reproducibility_hash="new_hash",
            assertions=[
                {
                    "assertion_kind": "install_success",
                    "command": "pip install verify-f",
                    "exit_code": 0,
                    "passed": True,
                    "reason": "",
                    "duration_ms": 2000,
                },
                {
                    "assertion_kind": "import_success",
                    "command": "python -c 'import verify_f'",
                    "exit_code": 0,
                    "passed": True,
                    "reason": "",
                    "duration_ms": 500,
                },
            ],
        )

        result = queries.verification_status(conn, str(cap))
        assert result is not None
        assert result["verified"] is True
        assert len(result["runs"]) == 2
        assert result["runs"][0]["result"] == "passed"
        assert result["runs"][1]["result"] == "failed"

    def test_failed_latest_run_means_not_verified(self, conn):
        cap = _mk_capability(conn, "pypi:verify-g", "verify-g")
        ver = _mk_version(conn, cap)
        conn.commit()

        queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="passed",
            duration_ms=3000,
            assertions=[],
        )

        queries.record_verification_run(
            conn,
            capability_version_id=str(ver),
            result="failed",
            duration_ms=2000,
            assertions=[
                {
                    "assertion_kind": "install_success",
                    "command": "pip install verify-g",
                    "exit_code": 1,
                    "passed": False,
                    "reason": "exit code 1",
                    "duration_ms": 2000,
                },
            ],
        )

        result = queries.verification_status(conn, str(cap))
        assert result["verified"] is False
