"""
FAIR-powered PDR analysis for CIP.

Uses FAIR's free AI inference to drive CIP's PDR-to-Build workflow:
  - extract_requirements: read a PDR and produce structured requirements
  - suggest_verdict: evaluate search candidates and recommend a verdict
  - analyze_source: analyze source code for capability extraction
"""
from __future__ import annotations

import json
from typing import Any

from integrations.fair_client import FairClient, FairResponse


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
                "required": ["text", "priority", "acceptance_criteria", "source_span"],
                "properties": {
                    "text": {"type": "string"},
                    "priority": {"type": "string", "enum": ["must", "should", "could", "wont"]},
                    "acceptance_criteria": {"type": "string"},
                    "source_span": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                            "quote": {"type": "string"},
                        },
                    },
                    "constraints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "detail": {"type": "object"},
                            },
                        },
                    },
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
            "enum": ["ADOPT", "ADAPT", "WRAP", "REFERENCE", "REJECT", "BUILD"],
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
- text: a clear one-sentence description of the requirement
- priority: one of "must", "should", "could", "wont" (MoSCoW)
- acceptance_criteria: a testable condition that proves this requirement is met
- source_span: an object with "start" (approximate character offset), "end", and "quote" (the exact phrase from the PDR)
- constraints: optional array of technical constraints (e.g. {{"kind": "required_interface", "detail": {{"name": "authenticate"}}}})

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
- REJECT: none of the candidates are suitable
- BUILD: build this from scratch (only if no candidate is viable)

Requirement:
  ID: {req_id}
  Description: {description}
  Constraints: {constraints}

Candidates:
{candidates_text}

Evaluate each candidate's fit_score, blocking_gap_count, and intrinsic_score. Choose the best option.

Return a JSON object with:
- "verdict": one of ADOPT/ADAPT/WRAP/REFERENCE/REJECT/BUILD
- "rationale": a clear one-sentence explanation
- "chosen_capability_version_id": the UUID of the chosen capability version (null for REJECT/BUILD)

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
        return {
            "error": "FAIR returned non-JSON output",
            "fair_request_id": response.request_id,
            "raw_output": response.output,
            "requirements": [],
        }

    return {
        "requirements": parsed.get("requirements", []) if parsed else [],
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
                f"   fit_score={c.get('fit_score', 0):.3f}, "
                f"blocking_gaps={c.get('blocking_gap_count', '?')}, "
                f"intrinsic_score={c.get('intrinsic_score', 'n/a')}"
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
        return {
            "error": "FAIR returned non-JSON output",
            "fair_request_id": response.request_id,
            "raw_output": response.output,
        }

    if parsed is None:
        return {"error": "Empty response", "fair_request_id": response.request_id}

    return {
        "verdict": parsed.get("verdict", "BUILD"),
        "rationale": parsed.get("rationale", ""),
        "chosen_capability_version_id": parsed.get("chosen_capability_version_id"),
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
        return {
            "error": "FAIR returned non-JSON output",
            "fair_request_id": response.request_id,
            "capabilities": [],
        }

    return {
        "capabilities": parsed.get("capabilities", []) if parsed else [],
        "fair_request_id": response.request_id,
        "provider_id": response.provider_id,
        "model_id": response.model_id,
    }
