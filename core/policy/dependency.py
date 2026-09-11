"""
Dependency fit evaluator (Phase 2).

Pure function: given a capability's dependency facts and an environment
profile, produce a DependencyFitResult listing hard failures, warnings,
and an overall pass/fail.

No DB, no IO, no LLM.

Fact kinds and how they match against the profile:

  runtime_version   fact_key='python', version_spec='>=3.9'
                    Profile must declare runtimes.{fact_key} and the
                    declared version must satisfy version_spec.

  os                fact_key='linux' | 'darwin' | 'win32'
                    Profile must list fact_key in its os array.

  system_library    fact_key='libpq' | 'ffmpeg' | ...
                    Profile must list fact_key in system_libraries.

  service           fact_key='postgresql' | 'redis' | 's3' | ...
                    Profile must list fact_key in services.

  environment_var   fact_key='DATABASE_URL' | 'API_KEY' | ...
                    Profile must list fact_key in environment_vars.

  hardware          fact_key='gpu' | 'min_ram_gb:8' | 'arch:x86_64'
                    Profile.hardware checked accordingly.

When profile data is missing for a check, the result is a warning
(not a hard failure) — we don't over-reject incomplete profiles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    HARD = "hard"
    WARNING = "warning"


@dataclass(frozen=True)
class DependencyFinding:
    fact_kind: str
    fact_key: str
    severity: Severity
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class DependencyFact:
    fact_kind: str
    fact_key: str
    version_spec: str = ""
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DependencyFitResult:
    passed: bool
    hard_failures: list[DependencyFinding] = field(default_factory=list)
    warnings: list[DependencyFinding] = field(default_factory=list)

    @property
    def findings(self) -> list[DependencyFinding]:
        return self.hard_failures + self.warnings


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def _parse_version(v: str) -> tuple[int, ...]:
    m = _VERSION_RE.search(v.strip())
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def _compare_versions(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    for x, y in zip(a, b):
        if x < y:
            return -1
        if x > y:
            return 1
    if len(a) < len(b):
        return -1
    if len(a) > len(b):
        return 1
    return 0


_SPEC_RE = re.compile(r"(>=|<=|>|<|==|!=|~=)\s*(\d+(?:\.\d+)*)")


def version_satisfies(have: str, spec: str) -> bool:
    """Check if version `have` satisfies a PEP 440-like version spec."""
    if not spec or not spec.strip():
        return True

    have_t = _parse_version(have)
    if not have_t:
        return False

    for m in _SPEC_RE.finditer(spec):
        op, ver_str = m.group(1), m.group(2)
        want_t = _parse_version(ver_str)
        cmp = _compare_versions(have_t, want_t)

        if op == ">=" and cmp < 0:
            return False
        elif op == "<=" and cmp > 0:
            return False
        elif op == ">" and cmp <= 0:
            return False
        elif op == "<" and cmp >= 0:
            return False
        elif op == "==" and cmp != 0:
            return False
        elif op == "!=" and cmp == 0:
            return False
        elif op == "~=":
            if cmp < 0:
                return False
            if len(want_t) >= 2:
                upper = want_t[:-1] + (want_t[-2] + 1,) if len(want_t) >= 2 else ()
                if upper and _compare_versions(have_t, upper) >= 0:
                    return False

    return True


# ---------------------------------------------------------------------------
# Individual fact checkers
# ---------------------------------------------------------------------------

def _check_runtime_version(
    fact: DependencyFact, profile: dict[str, Any],
) -> DependencyFinding | None:
    runtimes = profile.get("runtimes") or {}
    have = runtimes.get(fact.fact_key)
    if have is None:
        if fact.required:
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "runtime_not_declared",
                f"profile does not declare {fact.fact_key} version",
            )
        return None
    if not version_satisfies(str(have), fact.version_spec):
        sev = Severity.HARD if fact.required else Severity.WARNING
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, sev,
            "version_mismatch",
            f"need {fact.fact_key}{fact.version_spec}, have {have}",
        )
    return None


def _check_os(
    fact: DependencyFact, profile: dict[str, Any],
) -> DependencyFinding | None:
    os_list = [s.lower() for s in (profile.get("os") or [])]
    if not os_list:
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, Severity.WARNING,
            "os_not_declared",
            "profile does not declare OS",
        )
    if fact.fact_key.lower() not in os_list:
        sev = Severity.HARD if fact.required else Severity.WARNING
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, sev,
            "os_mismatch",
            f"needs {fact.fact_key}, profile has {os_list}",
        )
    return None


def _check_system_library(
    fact: DependencyFact, profile: dict[str, Any],
) -> DependencyFinding | None:
    libs = [s.lower() for s in (profile.get("system_libraries") or [])]
    if not libs:
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, Severity.WARNING,
            "system_libraries_not_declared",
            "profile does not declare system libraries",
        )
    if fact.fact_key.lower() not in libs:
        sev = Severity.HARD if fact.required else Severity.WARNING
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, sev,
            "missing_system_library",
            f"needs {fact.fact_key}",
        )
    return None


def _check_service(
    fact: DependencyFact, profile: dict[str, Any],
) -> DependencyFinding | None:
    services = [s.lower() for s in (profile.get("services") or [])]
    if not services:
        if fact.required:
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "services_not_declared",
                "profile does not declare available services",
            )
        return None
    if fact.fact_key.lower() not in services:
        sev = Severity.HARD if fact.required else Severity.WARNING
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, sev,
            "missing_service",
            f"needs {fact.fact_key}",
        )
    return None


def _check_environment_var(
    fact: DependencyFact, profile: dict[str, Any],
) -> DependencyFinding | None:
    env_vars = [s for s in (profile.get("environment_vars") or [])]
    if not env_vars:
        if fact.required:
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "env_vars_not_declared",
                "profile does not declare environment variables",
            )
        return None
    if fact.fact_key not in env_vars:
        sev = Severity.HARD if fact.required else Severity.WARNING
        return DependencyFinding(
            fact.fact_kind, fact.fact_key, sev,
            "missing_env_var",
            f"needs {fact.fact_key}",
        )
    return None


def _check_hardware(
    fact: DependencyFact, profile: dict[str, Any],
) -> DependencyFinding | None:
    hw = profile.get("hardware") or {}
    key = fact.fact_key.lower()

    if key == "gpu":
        if not hw:
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "hardware_not_declared",
                "profile does not declare hardware",
            )
        if not hw.get("gpu", False):
            sev = Severity.HARD if fact.required else Severity.WARNING
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, sev,
                "no_gpu",
                "capability requires GPU but profile has gpu=false",
            )
        return None

    if key.startswith("min_ram_gb:"):
        try:
            need = float(key.split(":", 1)[1])
        except ValueError:
            return None
        have = hw.get("min_ram_gb")
        if have is None:
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "ram_not_declared",
                "profile does not declare min_ram_gb",
            )
        if float(have) < need:
            sev = Severity.HARD if fact.required else Severity.WARNING
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, sev,
                "insufficient_ram",
                f"needs {need}GB, profile has {have}GB",
            )
        return None

    if key.startswith("arch:"):
        want_arch = key.split(":", 1)[1]
        arch_list = [s.lower() for s in (profile.get("arch") or [])]
        if not arch_list:
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "arch_not_declared",
                "profile does not declare architecture",
            )
        if want_arch.lower() not in arch_list:
            sev = Severity.HARD if fact.required else Severity.WARNING
            return DependencyFinding(
                fact.fact_kind, fact.fact_key, sev,
                "arch_mismatch",
                f"needs {want_arch}, profile has {arch_list}",
            )
        return None

    return None


_CHECKERS = {
    "runtime_version": _check_runtime_version,
    "os": _check_os,
    "system_library": _check_system_library,
    "service": _check_service,
    "environment_var": _check_environment_var,
    "hardware": _check_hardware,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_dependency_fit(
    facts: list[DependencyFact],
    profile: dict[str, Any],
) -> DependencyFitResult:
    """
    Check whether a capability's dependency facts are satisfiable in
    the given environment profile.

    Returns a DependencyFitResult with hard_failures, warnings, and
    an overall pass/fail. passed=True means no hard failures.
    """
    hard: list[DependencyFinding] = []
    warns: list[DependencyFinding] = []

    for fact in facts:
        checker = _CHECKERS.get(fact.fact_kind)
        if checker is None:
            warns.append(DependencyFinding(
                fact.fact_kind, fact.fact_key, Severity.WARNING,
                "unknown_fact_kind",
                f"no checker for {fact.fact_kind}",
            ))
            continue
        finding = checker(fact, profile)
        if finding is None:
            continue
        if finding.severity == Severity.HARD:
            hard.append(finding)
        else:
            warns.append(finding)

    return DependencyFitResult(
        passed=len(hard) == 0,
        hard_failures=hard,
        warnings=warns,
    )


def detect_version_conflicts(
    deps_a: list[dict[str, str]],
    deps_b: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """
    Detect package-level version conflicts between two dependency sets.

    Each dep is {ecosystem, name, version_spec}. Returns a list of
    conflict dicts for packages that appear in both sets with
    incompatible version specs.
    """
    conflicts: list[dict[str, Any]] = []

    index_b: dict[tuple[str, str], str] = {}
    for d in deps_b:
        key = (d["ecosystem"].lower(), d["name"].lower())
        index_b[key] = d.get("version_spec", "")

    for d in deps_a:
        key = (d["ecosystem"].lower(), d["name"].lower())
        spec_b = index_b.get(key)
        if spec_b is None:
            continue
        spec_a = d.get("version_spec", "")
        if not spec_a or not spec_b:
            continue
        if _specs_conflict(spec_a, spec_b):
            conflicts.append({
                "ecosystem": key[0],
                "name": key[1],
                "spec_a": spec_a,
                "spec_b": spec_b,
                "reason": "incompatible version ranges",
            })

    return conflicts


def _specs_conflict(spec_a: str, spec_b: str) -> bool:
    """
    Heuristic conflict detection: check if the two specs define
    non-overlapping ranges. Conservative — returns False (no conflict)
    when uncertain.
    """
    lower_a = _extract_lower_bound(spec_a)
    upper_a = _extract_upper_bound(spec_a)
    lower_b = _extract_lower_bound(spec_b)
    upper_b = _extract_upper_bound(spec_b)

    if lower_a and upper_b:
        if _compare_versions(lower_a, upper_b) > 0:
            return True
    if lower_b and upper_a:
        if _compare_versions(lower_b, upper_a) > 0:
            return True

    return False


def _extract_lower_bound(spec: str) -> tuple[int, ...] | None:
    for m in _SPEC_RE.finditer(spec):
        op, ver = m.group(1), m.group(2)
        if op in (">=", ">", "==", "~="):
            return _parse_version(ver)
    return None


def _extract_upper_bound(spec: str) -> tuple[int, ...] | None:
    for m in _SPEC_RE.finditer(spec):
        op, ver = m.group(1), m.group(2)
        if op in ("<", "<="):
            return _parse_version(ver)
        if op == "==":
            return _parse_version(ver)
    return None
