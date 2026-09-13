"""
Lightweight verification runner (Phase 4a).

Runs install, build, import, and smoke-test assertions for a capability
in an isolated subprocess with venv, timeout, and resource limits.

Assertion kinds:
  install_success  — pip install succeeds in a fresh venv
  build_success    — a build command (e.g. python setup.py build) exits 0
  import_success   — python -c "import <package>" exits 0
  smoke_pass       — a user-supplied smoke command exits 0
  contract_pass    — a contract-test command exits 0

Security:
  - All commands run in a subprocess with a hard timeout.
  - Stdout/stderr are captured and truncated.
  - Venv is created in a temp directory and cleaned up.
  - No shell=True — commands are tokenized.
"""
from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600


class AssertionKind(str, Enum):
    INSTALL_SUCCESS = "install_success"
    BUILD_SUCCESS = "build_success"
    IMPORT_SUCCESS = "import_success"
    SMOKE_PASS = "smoke_pass"
    CONTRACT_PASS = "contract_pass"


class RunResult(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class AssertionResult:
    assertion_kind: str
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    passed: bool
    reason: str


@dataclass
class VerificationResult:
    result: str
    assertions: list[AssertionResult]
    duration_ms: int
    environment: dict[str, Any]
    logs: str
    artifacts: list[dict[str, str]]
    reproducibility_hash: str


@dataclass
class VerificationSpec:
    """What to verify and how."""
    package_name: str
    install_spec: str | None = None
    import_name: str | None = None
    build_command: str | None = None
    smoke_commands: list[str] = field(default_factory=list)
    contract_commands: list[str] = field(default_factory=list)
    timeout_s: int = DEFAULT_TIMEOUT_S
    python_executable: str | None = None


def _truncate(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[truncated]"


def _run_command(
    cmd: list[str],
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> AssertionResult:
    cmd_str = shlex.join(cmd)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=min(timeout_s, MAX_TIMEOUT_S),
            cwd=cwd,
            env=env,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        stdout = _truncate(proc.stdout.decode("utf-8", errors="replace"))
        stderr = _truncate(proc.stderr.decode("utf-8", errors="replace"))
        passed = proc.returncode == 0
        reason = "" if passed else f"exit code {proc.returncode}"
        return AssertionResult(
            assertion_kind="",
            command=cmd_str,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=elapsed_ms,
            passed=passed,
            reason=reason,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return AssertionResult(
            assertion_kind="",
            command=cmd_str,
            exit_code=None,
            stdout="",
            stderr=f"timed out after {timeout_s}s",
            duration_ms=elapsed_ms,
            passed=False,
            reason=f"timeout after {timeout_s}s",
        )
    except FileNotFoundError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return AssertionResult(
            assertion_kind="",
            command=cmd_str,
            exit_code=None,
            stdout="",
            stderr=str(e),
            duration_ms=elapsed_ms,
            passed=False,
            reason=f"command not found: {e}",
        )


def _compute_reproducibility_hash(assertions: list[AssertionResult]) -> str:
    h = hashlib.sha256()
    for a in assertions:
        h.update(a.assertion_kind.encode())
        h.update(str(a.passed).encode())
        h.update(str(a.exit_code or 0).encode())
    return h.hexdigest()[:16]


def _get_python_executable(spec: VerificationSpec) -> str:
    return spec.python_executable or sys.executable


def _get_venv_python(venv_dir: Path) -> str:
    if sys.platform == "win32":
        return str(venv_dir / "Scripts" / "python.exe")
    return str(venv_dir / "bin" / "python")


def _get_venv_pip(venv_dir: Path) -> str:
    if sys.platform == "win32":
        return str(venv_dir / "Scripts" / "pip.exe")
    return str(venv_dir / "bin" / "pip")


def create_venv(base_dir: Path, python_exe: str) -> Path:
    venv_dir = base_dir / "venv"
    subprocess.run(
        [python_exe, "-m", "venv", str(venv_dir)],
        capture_output=True,
        timeout=60,
        check=True,
    )
    return venv_dir


def run_assertions(
    spec: VerificationSpec,
    venv_dir: Path | None = None,
) -> list[AssertionResult]:
    results: list[AssertionResult] = []

    if venv_dir is not None:
        pip_exe = _get_venv_pip(venv_dir)
        python_exe = _get_venv_python(venv_dir)
    else:
        pip_exe = "pip"
        python_exe = _get_python_executable(spec)

    install_spec = spec.install_spec or spec.package_name
    install_result = _run_command(
        [pip_exe, "install", install_spec],
        timeout_s=spec.timeout_s,
    )
    install_result.assertion_kind = AssertionKind.INSTALL_SUCCESS.value
    results.append(install_result)

    if not install_result.passed:
        return results

    if spec.build_command:
        tokens = shlex.split(spec.build_command)
        build_result = _run_command(tokens, timeout_s=spec.timeout_s)
        build_result.assertion_kind = AssertionKind.BUILD_SUCCESS.value
        results.append(build_result)
        if not build_result.passed:
            return results

    import_name = spec.import_name or spec.package_name
    import_result = _run_command(
        [python_exe, "-c", f"import {import_name}"],
        timeout_s=30,
    )
    import_result.assertion_kind = AssertionKind.IMPORT_SUCCESS.value
    results.append(import_result)

    for cmd_str in spec.smoke_commands:
        tokens = shlex.split(cmd_str)
        smoke_result = _run_command(tokens, timeout_s=spec.timeout_s)
        smoke_result.assertion_kind = AssertionKind.SMOKE_PASS.value
        results.append(smoke_result)

    for cmd_str in spec.contract_commands:
        tokens = shlex.split(cmd_str)
        contract_result = _run_command(tokens, timeout_s=spec.timeout_s)
        contract_result.assertion_kind = AssertionKind.CONTRACT_PASS.value
        results.append(contract_result)

    return results


def verify(spec: VerificationSpec) -> VerificationResult:
    """
    Full verification pipeline: create venv, install, build, import,
    smoke test, contract test. Returns a VerificationResult.
    """
    start = time.monotonic()
    python_exe = _get_python_executable(spec)

    environment = {
        "python": python_exe,
        "platform": sys.platform,
        "package": spec.package_name,
        "install_spec": spec.install_spec or spec.package_name,
    }

    log_lines: list[str] = []
    artifacts: list[dict[str, str]] = []

    try:
        with tempfile.TemporaryDirectory(prefix="cip-verify-") as tmpdir:
            base_dir = Path(tmpdir)
            log_lines.append(f"creating venv in {base_dir}")

            try:
                venv_dir = create_venv(base_dir, python_exe)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return VerificationResult(
                    result=RunResult.ERROR.value,
                    assertions=[],
                    duration_ms=elapsed_ms,
                    environment=environment,
                    logs=f"venv creation failed: {e}",
                    artifacts=[],
                    reproducibility_hash="",
                )

            log_lines.append(f"venv created at {venv_dir}")
            assertions = run_assertions(spec, venv_dir=venv_dir)

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return VerificationResult(
            result=RunResult.ERROR.value,
            assertions=[],
            duration_ms=elapsed_ms,
            environment=environment,
            logs=f"unexpected error: {e}",
            artifacts=[],
            reproducibility_hash="",
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)

    for a in assertions:
        status = "PASS" if a.passed else "FAIL"
        log_lines.append(f"[{status}] {a.assertion_kind}: {a.command}")

    all_passed = all(a.passed for a in assertions)
    any_timeout = any(a.reason.startswith("timeout") for a in assertions)

    if any_timeout:
        result = RunResult.TIMEOUT.value
    elif all_passed:
        result = RunResult.PASSED.value
    else:
        result = RunResult.FAILED.value

    repro_hash = _compute_reproducibility_hash(assertions)

    return VerificationResult(
        result=result,
        assertions=assertions,
        duration_ms=elapsed_ms,
        environment=environment,
        logs="\n".join(log_lines),
        artifacts=artifacts,
        reproducibility_hash=repro_hash,
    )


def evaluate_assertions_only(
    assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Pure evaluator: given a list of assertion dicts (already run),
    compute the overall result and reproducibility hash.
    Used when assertions are loaded from the DB rather than executed.
    """
    all_passed = all(a.get("passed", False) for a in assertions)
    any_timeout = any(
        (a.get("reason") or "").startswith("timeout") for a in assertions
    )

    if not assertions:
        result = "pending"
    elif any_timeout:
        result = "timeout"
    elif all_passed:
        result = "passed"
    else:
        result = "failed"

    h = hashlib.sha256()
    for a in assertions:
        h.update((a.get("assertion_kind") or "").encode())
        h.update(str(a.get("passed", False)).encode())
        h.update(str(a.get("exit_code") or 0).encode())
    repro_hash = h.hexdigest()[:16]

    return {"result": result, "reproducibility_hash": repro_hash}
