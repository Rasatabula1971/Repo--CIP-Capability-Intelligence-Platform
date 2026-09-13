"""
Requirement YAML parser (Phase 7).

Parses structured YAML requirement files into a list of Requirement
objects that can be persisted to project_requirement +
requirement_constraint rows.

YAML format:
  requirements:
    - slug: http-client
      description: "Need an HTTP client library"
      constraints:
        - kind: required_interface
          name: get
          signature_contains: "url"
        - kind: license_allowlist
          spdx_ids: [MIT, Apache-2.0]
        - kind: forbidden_dependency
          ecosystem: pypi
          name: urllib3

Each requirement must have a unique slug within the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.project.types import Constraint, Requirement


class RequirementParseError(ValueError):
    """Raised when a requirement YAML file is malformed."""


def parse_requirements_yaml(text: str) -> list[dict[str, Any]]:
    """
    Parse YAML text into a list of requirement dicts ready for DB insert.

    Returns list of:
      {slug, description, constraints: [{kind, detail}]}

    Raises RequirementParseError on validation failures.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RequirementParseError(f"invalid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise RequirementParseError("top-level must be a mapping")

    reqs_raw = raw.get("requirements")
    if not isinstance(reqs_raw, list):
        raise RequirementParseError("'requirements' must be a list")

    if not reqs_raw:
        raise RequirementParseError("'requirements' list is empty")

    seen_slugs: set[str] = set()
    results: list[dict[str, Any]] = []

    for i, entry in enumerate(reqs_raw):
        if not isinstance(entry, dict):
            raise RequirementParseError(
                f"requirement #{i+1}: must be a mapping"
            )

        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise RequirementParseError(
                f"requirement #{i+1}: 'slug' must be a non-empty string"
            )
        slug = slug.strip()

        if slug in seen_slugs:
            raise RequirementParseError(
                f"requirement #{i+1}: duplicate slug {slug!r}"
            )
        seen_slugs.add(slug)

        description = entry.get("description", "")
        if not isinstance(description, str):
            raise RequirementParseError(
                f"requirement {slug!r}: 'description' must be a string"
            )

        constraints_raw = entry.get("constraints", [])
        if not isinstance(constraints_raw, list):
            raise RequirementParseError(
                f"requirement {slug!r}: 'constraints' must be a list"
            )

        constraints: list[dict[str, Any]] = []
        for j, c in enumerate(constraints_raw):
            parsed = _parse_constraint(slug, j, c)
            constraints.append(parsed)

        results.append({
            "slug": slug,
            "description": description,
            "constraints": constraints,
        })

    return results


def parse_requirements_file(path: str | Path) -> list[dict[str, Any]]:
    """Load and parse a YAML file from disk."""
    p = Path(path)
    if not p.exists():
        raise RequirementParseError(f"file not found: {p}")
    return parse_requirements_yaml(p.read_text(encoding="utf-8"))


def to_domain_objects(
    parsed: list[dict[str, Any]],
    id_factory=None,
) -> list[Requirement]:
    """
    Convert parsed requirement dicts into domain Requirement objects.
    id_factory produces IDs (default: uuid4 hex strings).
    """
    import uuid
    if id_factory is None:
        id_factory = lambda: str(uuid.uuid4())

    requirements: list[Requirement] = []
    for req in parsed:
        constraints = tuple(
            Constraint(kind=c["kind"], detail=c["detail"])
            for c in req["constraints"]
        )
        requirements.append(Requirement(
            id=id_factory(),
            slug=req["slug"],
            description=req["description"],
            constraints=constraints,
        ))
    return requirements


_VALID_CONSTRAINT_KINDS = frozenset({
    "required_interface",
    "license_allowlist",
    "forbidden_dependency",
})


def _parse_constraint(
    slug: str, index: int, raw: Any,
) -> dict[str, Any]:
    """Parse one constraint entry, returning {kind, detail}."""
    if not isinstance(raw, dict):
        raise RequirementParseError(
            f"requirement {slug!r}, constraint #{index+1}: must be a mapping"
        )

    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _VALID_CONSTRAINT_KINDS:
        raise RequirementParseError(
            f"requirement {slug!r}, constraint #{index+1}: "
            f"'kind' must be one of {sorted(_VALID_CONSTRAINT_KINDS)}"
        )

    detail = _extract_detail(slug, index, kind, raw)
    return {"kind": kind, "detail": detail}


def _extract_detail(
    slug: str, index: int, kind: str, raw: dict,
) -> dict[str, Any]:
    """Extract and validate kind-specific detail fields."""
    if kind == "required_interface":
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RequirementParseError(
                f"requirement {slug!r}, constraint #{index+1}: "
                f"required_interface needs 'name'"
            )
        detail: dict[str, Any] = {"name": name.strip()}
        sig = raw.get("signature_contains")
        if sig is not None:
            detail["signature_contains"] = str(sig)
        return detail

    if kind == "license_allowlist":
        spdx_ids = raw.get("spdx_ids")
        if not isinstance(spdx_ids, list) or not spdx_ids:
            raise RequirementParseError(
                f"requirement {slug!r}, constraint #{index+1}: "
                f"license_allowlist needs non-empty 'spdx_ids' list"
            )
        return {"spdx_ids": [str(s) for s in spdx_ids]}

    if kind == "forbidden_dependency":
        ecosystem = raw.get("ecosystem")
        name = raw.get("name")
        if not isinstance(ecosystem, str) or not ecosystem.strip():
            raise RequirementParseError(
                f"requirement {slug!r}, constraint #{index+1}: "
                f"forbidden_dependency needs 'ecosystem'"
            )
        if not isinstance(name, str) or not name.strip():
            raise RequirementParseError(
                f"requirement {slug!r}, constraint #{index+1}: "
                f"forbidden_dependency needs 'name'"
            )
        return {"ecosystem": ecosystem.strip(), "name": name.strip()}

    return {}
