"""
FAIR-powered PDR analysis for CIP.

Uses FAIR's free AI inference to help with CIP's PDR-to-Build workflow:
  - extract_requirements: read a PDR and draft structured requirements
  - suggest_verdict: evaluate search candidates and suggest a verdict
  - analyze_source: analyze source code for capability extraction

These write nothing and decide nothing. What comes back is a draft from a
free model, in the shape the workflow's own tools take (spec v1.1):
requirement drafts go through record_requirements, where CIP locates
every quote in the stored PDR and refuses what it cannot find; a
suggested verdict goes to record_decision as the caller's proposal,
where CIP computes its own and labels who decided.
"""
from __future__ import annotations

import json
from typing import Any

from integrations.fair_client import FairClient

# The verdicts record_decision accepts for a capability need.
VERDICTS = ("ADOPT", "ADAPT", "WRAP", "REFERENCE", "BUILD")


# ---------------------------------------------------------------------------
# JSON schemas for structured FAIR responses
# ---------------------------------------------------------------------------

_REQUIREMENTS_SCHEMA = {
    "type": "object",
    "required": ["requirements"],
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                # The shape record_requirements takes. No character offsets:
                # CIP computes the span from the quote. No constraints: in
                # v1.1 those belong to capability needs, not to requirements.
                "required": ["slug", "text", "priority", "kind", "quote",
                             "acceptance_criteria"],
                "properties": {
                    "slug": {"type": "string"},
                    "text": {"type": "string"},
                    "priority": {"type": "string", "enum": ["must", "should", "could", "wont"]},
                    "kind": {"type": "string",
                             "enum": ["functional", "non_functional", "constraint"]},
                    "quote": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_VERDICT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "rationale"],
    "properties": {
        "verdict": {
            "type": "string",
            # No REJECT: it is a statement about candidates, not an outcome
            # for a need. If nothing fits, the need is built.
            "enum": list(VERDICTS),
        },
        "rationale": {"type": "string"},
        "chosen_capability_version_id": {"type": ["string", "null"]},
    },
}

_CAPABILITY_SCHEMA = {
    "type": "object",
    "required": ["capabilities"],
    "properties": {
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "kind", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "description": {"type": "string"},
                    "interfaces": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "signature": {"type": "string"},
                                "kind": {"type": "string"},
                            },
                        },
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_EXTRACT_REQUIREMENTS_PROMPT = """You are a requirements analyst. Read the following Product Design Requirements (PDR) document and extract every discrete requirement.

For each requirement, produce:
- slug: a short unique identifier in lower-case words joined by hyphens (e.g. "staff-sign-in")
- text: a clear one-sentence description of the requirement, on one line
- priority: one of "must", "should", "could", "wont" (MoSCoW)
- kind: one of "functional", "non_functional", "constraint"
- quote: the one sentence in the PDR that states this requirement, copied character for character. At most 600 characters. It must come from a single section: never continue a quote across a heading. Do not paraphrase, shorten or correct it
- acceptance_criteria: a list of testable conditions, each on one line, that together prove this requirement is met

One requirement per item. Do not give character offsets.

Return a JSON object with a "requirements" array.

PDR document:
---
{pdr_text}
---

Respond with only valid JSON, no explanation."""

_SUGGEST_VERDICT_PROMPT = """You are a software architecture advisor. Given a requirement and a list of candidate capabilities from a registry, recommend a verdict.

Verdicts:
- ADOPT: use the capability as-is, it fully satisfies the requirement
- ADAPT: the capability mostly works but needs minor changes
- WRAP: build a thin wrapper around the capability
- REFERENCE: reuse ideas or patterns from it, don't take a dependency
- BUILD: build this from scratch (when no candidate is viable)

Requirement:
  ID: {req_id}
  Description: {description}
  Constraints: {constraints}

Candidates:
{candidates_text}

Evaluate each candidate's fit_score, blocking gaps, and intrinsic_score. Choose the best option.

How to read a candidate:
- evidence=insufficient means the registry has NOT read what a constraint depends on (the "missing" list says what: license, interfaces, dependencies, score). Its fit_score and blocking_gaps then say "not read", not "has a problem" and not "fits". Do not treat such a candidate as verified either way, and say so in the rationale if you choose it.
- confirmed_gaps are gaps that rest on something the registry did read (for example a license that is not allowed). Those are real.

Return a JSON object with:
- "verdict": one of ADOPT/ADAPT/WRAP/REFERENCE/BUILD
- "rationale": a clear one-sentence explanation
- "chosen_capability_version_id": the UUID of the chosen capability version (null for BUILD)

Respond with only valid JSON, no explanation."""

_ANALYZE_SOURCE_PROMPT = """You are a code analyst. Analyze the following source code and identify the capabilities it provides.

For each capability found, extract:
- name: a descriptive name (e.g. "HTTP Client", "OAuth2 Authentication")
- kind: the type (library, service, cli, framework, utility)
- description: what it does in one sentence
- interfaces: public functions/classes with their signatures
- dependencies: external packages it imports

File: {file_path}
```
{source_code}
```

Return a JSON object with a "capabilities" array. Respond with only valid JSON, no explanation."""


def _listed(values: Any) -> str:
    return ", ".join(str(v) for v in values) if values else "none"


def _license(candidate: dict) -> str:
    """A candidate's license with where it came from: metadata is not a reading."""
    license_ = candidate.get("license")
    if not license_:
        return "not read"
    source = {
        "evidence": "read from source",
        "metadata": "registry metadata, not read from source",
    }.get(candidate.get("license_source") or "", "source not stated")
    return f"{license_} ({source})"


# ---------------------------------------------------------------------------
# Driver functions
# ---------------------------------------------------------------------------

def extract_requirements(
    client: FairClient,
    pdr_text: str,
) -> dict[str, Any]:
    prompt = _EXTRACT_REQUIREMENTS_PROMPT.format(pdr_text=pdr_text)

    response = client.solve(
        task=prompt,
        task_type="extraction",
        quality_level="standard",
        expected_schema=_REQUIREMENTS_SCHEMA,
    )

    if not response.accepted:
        return {
            "error": f"FAIR returned {response.status}: {response.reason_code}",
            "fair_request_id": response.request_id,
            "requirements": [],
        }

    try:
        parsed = response.output_json()
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if not isinstance(parsed, dict):
        return {
            "error": "FAIR returned non-object JSON output",
            "fair_request_id": response.request_id,
            "raw_output": response.output,
            "requirements": [],
        }

    return {
        "requirements": parsed.get("requirements", []),
        "fair_request_id": response.request_id,
        "provider_id": response.provider_id,
        "model_id": response.model_id,
        "verification_state": response.verification_state,
    }


def suggest_verdict(
    client: FairClient,
    *,
    req_id: str,
    description: str,
    constraints: list[dict],
    candidates: list[dict],
) -> dict[str, Any]:
    if not candidates:
        candidates_text = "(No candidates found in the registry)"
    else:
        lines = []
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"{i}. {c.get('display_name', 'Unknown')} "
                f"(key={c.get('normalized_key', '?')}, "
                f"version_id={c.get('capability_version_id', '?')}, "
                f"ecosystem={c.get('ecosystem', '?')})\n"
                f"   fit_score={(c.get('fit_score') or 0):.3f}, "
                f"blocking_gaps={c.get('blocking_gap_count', '?')}, "
                f"intrinsic_score={c.get('intrinsic_score', 'n/a')}\n"
                # What search_for_requirement says about how far to trust
                # those numbers. Absent on a candidate from an older caller.
                f"   evidence={c.get('evidence', 'not stated')}, "
                f"missing={_listed(c.get('missing_evidence'))}, "
                f"confirmed_gaps={_listed(c.get('confirmed_blocking_gaps'))}, "
                f"license={_license(c)}"
            )
        candidates_text = "\n".join(lines)

    prompt = _SUGGEST_VERDICT_PROMPT.format(
        req_id=req_id,
        description=description,
        constraints=json.dumps(constraints),
        candidates_text=candidates_text,
    )

    response = client.solve(
        task=prompt,
        task_type="classification",
        quality_level="standard",
        expected_schema=_VERDICT_SCHEMA,
    )

    if not response.accepted:
        return {
            "error": f"FAIR returned {response.status}: {response.reason_code}",
            "fair_request_id": response.request_id,
        }

    try:
        parsed = response.output_json()
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if not isinstance(parsed, dict):
        return {
            "error": "FAIR returned non-object JSON output",
            "fair_request_id": response.request_id,
            "raw_output": response.output,
        }

    verdict = str(parsed.get("verdict") or "BUILD").strip().upper()
    chosen = parsed.get("chosen_capability_version_id")
    note = None
    if verdict not in VERDICTS:
        # A model that answers REJECT (or anything else) means "none of
        # these". For a need that is BUILD, which is what the workflow
        # would turn it into anyway; said here, not done silently.
        note = f"the model answered {verdict!r}, which is not a verdict for a need; read as BUILD"
        verdict, chosen = "BUILD", None
    elif verdict == "BUILD":
        chosen = None

    return {
        "verdict": verdict,
        "rationale": parsed.get("rationale", ""),
        "chosen_capability_version_id": chosen,
        "note": note,
        "fair_request_id": response.request_id,
        "provider_id": response.provider_id,
        "model_id": response.model_id,
        "verification_state": response.verification_state,
    }


def analyze_source(
    client: FairClient,
    source_code: str,
    file_path: str = "<unknown>",
) -> dict[str, Any]:
    prompt = _ANALYZE_SOURCE_PROMPT.format(
        file_path=file_path,
        source_code=source_code[:50000],
    )

    response = client.solve(
        task=prompt,
        task_type="extraction",
        quality_level="standard",
        expected_schema=_CAPABILITY_SCHEMA,
    )

    if not response.accepted:
        return {
            "error": f"FAIR returned {response.status}: {response.reason_code}",
            "fair_request_id": response.request_id,
            "capabilities": [],
        }

    try:
        parsed = response.output_json()
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if not isinstance(parsed, dict):
        return {
            "error": "FAIR returned non-object JSON output",
            "fair_request_id": response.request_id,
            "capabilities": [],
        }

    return {
        "capabilities": parsed.get("capabilities", []),
        "fair_request_id": response.request_id,
        "provider_id": response.provider_id,
        "model_id": response.model_id,
    }
