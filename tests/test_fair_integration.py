"""Tests for the FAIR integration layer.

Uses mock responses — no live FAIR server needed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from integrations.fair_client import FairClient, FairResponse
from integrations.fair_pdr import (
    extract_requirements,
    suggest_verdict,
    analyze_source,
)


# These tests never touch Postgres; skip the session-wide migration fixture.
@pytest.fixture(scope="session", autouse=True)
def _apply_migrations():
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(
    *,
    status: str = "ACCEPTED",
    output: str | None = None,
    reason_code: str = "OK",
    provider_id: str = "groq",
    model_id: str = "llama-3.3-70b",
) -> FairResponse:
    return FairResponse(
        request_id="req-test-001",
        status=status,
        reason_code=reason_code,
        output=output,
        verification_state="VERIFIED",
        best_quality_score=0.85,
        provider_id=provider_id,
        model_id=model_id,
        attempts=[],
        raw={},
    )


def _mock_client(response: FairResponse) -> FairClient:
    client = MagicMock(spec=FairClient)
    client.solve.return_value = response
    client.is_available.return_value = True
    client.client_id = "cip"
    return client


# ---------------------------------------------------------------------------
# FairResponse unit tests
# ---------------------------------------------------------------------------

class TestFairResponse:
    def test_accepted_true(self):
        r = _make_response(status="ACCEPTED")
        assert r.accepted is True
        assert r.escalated is False

    def test_escalated(self):
        r = _make_response(status="ESCALATION_REQUIRED")
        assert r.accepted is False
        assert r.escalated is True

    def test_failed(self):
        r = _make_response(status="FAILED")
        assert r.accepted is False
        assert r.escalated is False

    def test_output_json_plain(self):
        r = _make_response(output='{"key": "value"}')
        assert r.output_json() == {"key": "value"}

    def test_output_json_with_code_fence(self):
        r = _make_response(output='```json\n{"key": "value"}\n```')
        assert r.output_json() == {"key": "value"}

    def test_output_json_none(self):
        r = _make_response(output=None)
        assert r.output_json() is None

    def test_output_json_empty(self):
        r = _make_response(output="")
        assert r.output_json() is None

    def test_output_json_invalid(self):
        r = _make_response(output="not json")
        with pytest.raises(json.JSONDecodeError):
            r.output_json()


# ---------------------------------------------------------------------------
# FairClient unit tests
# ---------------------------------------------------------------------------

class TestFairClient:
    def test_default_client_id(self):
        with patch.dict("os.environ", {}, clear=True):
            assert FairClient().client_id == "cip"

    def test_env_client_id(self):
        with patch.dict("os.environ", {"FAIR_CLIENT_ID": "test-client"}, clear=True):
            assert FairClient().client_id == "test-client"

    def test_explicit_overrides_env(self):
        with patch.dict("os.environ", {"FAIR_CLIENT_ID": "env"}, clear=True):
            assert FairClient(client_id="explicit").client_id == "explicit"

    def test_unavailable_without_providers(self):
        # Must report cleanly whether fair is missing or just has no keys.
        with patch.dict("os.environ", {}, clear=True):
            status = FairClient().status()
        assert status["available"] is False
        assert status["providers"] == []
        assert status["error"]


try:
    import fair as _fair  # noqa: F401
    _HAS_FAIR = True
except ImportError:
    _HAS_FAIR = False


@pytest.mark.skipif(not _HAS_FAIR, reason="fair-free-ai-router not installed")
class TestFairClientRoundTrip:
    """Drive the real FairClient through FAIR's offline MockAdapter."""

    def _client(self, text: str) -> FairClient:
        from fair.providers.mock import MockAdapter
        from fair.schemas.domain import ProviderSpec

        spec = ProviderSpec(
            provider_id="mock",
            access_class="FREE_LOCAL",
            status="ACTIVE",
            current_access_cost_usd=0,
            requires_paid_subscription=False,
            requires_credit_purchase=False,
            auto_billing_required=False,
            programmatic_access=True,
            production_eligibility=True,
            models=[{
                "model_id": "mock-model",
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }],
        )
        return FairClient(providers=[(spec, MockAdapter("mock", text=text))])

    def test_status_lists_providers(self):
        with patch.dict("os.environ", {}, clear=True):
            status = self._client("x").status()
        assert status["available"] is True
        assert status["error"] is None
        assert status["providers"][0]["provider_id"] == "mock"
        assert "mock-model" in status["providers"][0]["models"]

    def test_solve_maps_response(self):
        with patch.dict("os.environ", {}, clear=True):
            r = self._client('{"requirements": []}').solve(
                "extract", task_type="extraction",
                expected_schema={"type": "object"},
            )
        assert isinstance(r, FairResponse)
        assert r.request_id
        assert r.status in {"ACCEPTED", "ESCALATION_REQUIRED", "FAILED"}
        assert r.reason_code
        assert r.raw["request_id"] == r.request_id
        assert len(r.attempts) >= 1
        assert r.attempts[0]["provider_id"] == "mock"
        assert r.attempts[0]["model_id"] == "mock-model"

    def test_solve_inside_running_loop(self):
        # The FastMCP path: a sync tool invoked on the event-loop thread.
        with patch.dict("os.environ", {}, clear=True):
            client = self._client("hello")

            async def go():
                return client.solve("say hi")

            r = asyncio.run(go())
        assert isinstance(r, FairResponse)
        assert r.request_id


# ---------------------------------------------------------------------------
# extract_requirements tests
# ---------------------------------------------------------------------------

class TestExtractRequirements:
    def test_success(self):
        output = json.dumps({
            "requirements": [
                {
                    "text": "System must authenticate users via OAuth2",
                    "priority": "must",
                    "acceptance_criteria": "OAuth2 flow completes within 3 seconds",
                    "source_span": {"start": 0, "end": 50, "quote": "authenticate users"},
                },
                {
                    "text": "Dashboard should show real-time metrics",
                    "priority": "should",
                    "acceptance_criteria": "Metrics update within 5 seconds",
                    "source_span": {"start": 100, "end": 150, "quote": "real-time metrics"},
                },
            ]
        })
        client = _mock_client(_make_response(output=output))
        result = extract_requirements(client, "Some PDR text here")

        assert len(result["requirements"]) == 2
        assert result["requirements"][0]["priority"] == "must"
        assert result["requirements"][1]["text"] == "Dashboard should show real-time metrics"
        assert result["fair_request_id"] == "req-test-001"
        assert result["provider_id"] == "groq"

    def test_fair_failed(self):
        client = _mock_client(_make_response(
            status="FAILED", reason_code="NO_PROVIDERS",
        ))
        result = extract_requirements(client, "PDR text")

        assert "error" in result
        assert result["requirements"] == []
        assert "FAILED" in result["error"]

    def test_non_json_output(self):
        client = _mock_client(_make_response(output="This is not JSON"))
        result = extract_requirements(client, "PDR text")

        assert "error" in result
        assert result["requirements"] == []

    def test_valid_json_but_not_object(self):
        # LLM returns a bare array instead of an object — must not crash.
        client = _mock_client(_make_response(output='[{"text": "req1"}]'))
        result = extract_requirements(client, "PDR text")

        assert "error" in result
        assert result["requirements"] == []

    def test_code_fenced_output(self):
        output = '```json\n{"requirements": [{"text": "req1", "priority": "must", "acceptance_criteria": "works", "source_span": {"start": 0, "end": 5, "quote": "req"}}]}\n```'
        client = _mock_client(_make_response(output=output))
        result = extract_requirements(client, "PDR text")

        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["text"] == "req1"

    def test_solve_called_with_correct_params(self):
        client = _mock_client(_make_response(output='{"requirements": []}'))
        extract_requirements(client, "My PDR")

        client.solve.assert_called_once()
        call_kwargs = client.solve.call_args
        assert "My PDR" in call_kwargs.kwargs.get("task", call_kwargs.args[0] if call_kwargs.args else "")
        assert call_kwargs.kwargs.get("task_type") == "extraction"
        assert call_kwargs.kwargs.get("quality_level") == "standard"
        assert call_kwargs.kwargs.get("expected_schema") is not None


# ---------------------------------------------------------------------------
# suggest_verdict tests
# ---------------------------------------------------------------------------

class TestSuggestVerdict:
    def test_adopt_verdict(self):
        output = json.dumps({
            "verdict": "ADOPT",
            "rationale": "Excellent fit, no gaps",
            "chosen_capability_version_id": "cv-123",
        })
        client = _mock_client(_make_response(output=output))
        result = suggest_verdict(
            client,
            req_id="req-1",
            description="Need HTTP client",
            constraints=[],
            candidates=[{
                "display_name": "httpx",
                "normalized_key": "httpx",
                "capability_version_id": "cv-123",
                "ecosystem": "pypi",
                "fit_score": 0.95,
                "blocking_gap_count": 0,
                "intrinsic_score": 0.88,
            }],
        )

        assert result["verdict"] == "ADOPT"
        assert result["rationale"] == "Excellent fit, no gaps"
        assert result["chosen_capability_version_id"] == "cv-123"

    def test_build_verdict_no_candidates(self):
        output = json.dumps({
            "verdict": "BUILD",
            "rationale": "No candidates found in registry",
            "chosen_capability_version_id": None,
        })
        client = _mock_client(_make_response(output=output))
        result = suggest_verdict(
            client,
            req_id="req-2",
            description="Need custom widget",
            constraints=[],
            candidates=[],
        )

        assert result["verdict"] == "BUILD"
        assert result["chosen_capability_version_id"] is None

    def test_fair_escalation(self):
        client = _mock_client(_make_response(
            status="ESCALATION_REQUIRED", reason_code="QUALITY_THRESHOLD",
        ))
        result = suggest_verdict(
            client,
            req_id="req-3",
            description="Complex requirement",
            constraints=[],
            candidates=[],
        )

        assert "error" in result
        assert "ESCALATION_REQUIRED" in result["error"]

    def test_valid_json_but_not_object(self):
        client = _mock_client(_make_response(output='["ADOPT"]'))
        result = suggest_verdict(
            client,
            req_id="req-6",
            description="Need something",
            constraints=[],
            candidates=[],
        )

        assert "error" in result
        assert "verdict" not in result

    def test_constraints_formatted(self):
        client = _mock_client(_make_response(
            output='{"verdict": "REJECT", "rationale": "No match", "chosen_capability_version_id": null}',
        ))
        suggest_verdict(
            client,
            req_id="req-4",
            description="Need MIT-licensed lib",
            constraints=[{"kind": "license_allowlist", "spdx_ids": ["MIT"]}],
            candidates=[],
        )

        call_kwargs = client.solve.call_args
        task_text = call_kwargs.kwargs.get("task", call_kwargs.args[0] if call_kwargs.args else "")
        assert "license_allowlist" in task_text

    def test_multiple_candidates_formatted(self):
        client = _mock_client(_make_response(
            output='{"verdict": "ADAPT", "rationale": "Close enough", "chosen_capability_version_id": "cv-1"}',
        ))
        suggest_verdict(
            client,
            req_id="req-5",
            description="Need a logger",
            constraints=[],
            candidates=[
                {
                    "display_name": "loguru",
                    "normalized_key": "loguru",
                    "capability_version_id": "cv-1",
                    "ecosystem": "pypi",
                    "fit_score": 0.8,
                    "blocking_gap_count": 1,
                    "intrinsic_score": 0.75,
                },
                {
                    "display_name": "structlog",
                    "normalized_key": "structlog",
                    "capability_version_id": "cv-2",
                    "ecosystem": "pypi",
                    "fit_score": 0.7,
                    "blocking_gap_count": 0,
                    "intrinsic_score": 0.82,
                },
            ],
        )

        call_kwargs = client.solve.call_args
        task_text = call_kwargs.kwargs.get("task", call_kwargs.args[0] if call_kwargs.args else "")
        assert "loguru" in task_text
        assert "structlog" in task_text


# ---------------------------------------------------------------------------
# analyze_source tests
# ---------------------------------------------------------------------------

class TestAnalyzeSource:
    def test_success(self):
        output = json.dumps({
            "capabilities": [
                {
                    "name": "HTTP Client",
                    "kind": "library",
                    "description": "Async HTTP client for Python",
                    "interfaces": [
                        {"name": "get", "signature": "async def get(url: str) -> Response", "kind": "function"},
                    ],
                    "dependencies": ["httpx", "asyncio"],
                },
            ]
        })
        client = _mock_client(_make_response(output=output))
        result = analyze_source(client, "import httpx\n\nasync def get(url): ...", "client.py")

        assert len(result["capabilities"]) == 1
        assert result["capabilities"][0]["name"] == "HTTP Client"
        assert result["capabilities"][0]["kind"] == "library"
        assert len(result["capabilities"][0]["interfaces"]) == 1

    def test_failed(self):
        client = _mock_client(_make_response(status="FAILED", reason_code="TIMEOUT"))
        result = analyze_source(client, "code", "file.py")

        assert "error" in result
        assert result["capabilities"] == []

    def test_valid_json_but_not_object(self):
        client = _mock_client(_make_response(output='["cap1", "cap2"]'))
        result = analyze_source(client, "code", "file.py")

        assert "error" in result
        assert result["capabilities"] == []

    def test_truncates_large_source(self):
        large_code = "x" * 100000
        client = _mock_client(_make_response(output='{"capabilities": []}'))
        analyze_source(client, large_code, "big.py")

        call_kwargs = client.solve.call_args
        task_text = call_kwargs.kwargs.get("task", call_kwargs.args[0] if call_kwargs.args else "")
        assert len(task_text) < 60000

    def test_file_path_in_prompt(self):
        client = _mock_client(_make_response(output='{"capabilities": []}'))
        analyze_source(client, "code", "src/utils/helper.py")

        call_kwargs = client.solve.call_args
        task_text = call_kwargs.kwargs.get("task", call_kwargs.args[0] if call_kwargs.args else "")
        assert "src/utils/helper.py" in task_text


# ---------------------------------------------------------------------------
# Integration scenario: extract -> search -> verdict pipeline
# ---------------------------------------------------------------------------

class TestFairPipeline:
    def test_extract_then_suggest(self):
        extract_output = json.dumps({
            "requirements": [
                {
                    "text": "Need an HTTP client library",
                    "priority": "must",
                    "acceptance_criteria": "Can make GET and POST requests",
                    "source_span": {"start": 0, "end": 30, "quote": "HTTP client"},
                },
            ]
        })
        verdict_output = json.dumps({
            "verdict": "ADOPT",
            "rationale": "httpx fully satisfies the requirement",
            "chosen_capability_version_id": "cv-httpx-1",
        })

        extract_response = _make_response(output=extract_output)
        verdict_response = _make_response(output=verdict_output)

        client = MagicMock(spec=FairClient)
        client.solve.side_effect = [extract_response, verdict_response]

        reqs = extract_requirements(client, "We need an HTTP client library for API calls")
        assert len(reqs["requirements"]) == 1

        req = reqs["requirements"][0]
        verdict = suggest_verdict(
            client,
            req_id="req-pipeline-1",
            description=req["text"],
            constraints=[],
            candidates=[{
                "display_name": "httpx",
                "normalized_key": "httpx",
                "capability_version_id": "cv-httpx-1",
                "ecosystem": "pypi",
                "fit_score": 0.95,
                "blocking_gap_count": 0,
                "intrinsic_score": 0.88,
            }],
        )

        assert verdict["verdict"] == "ADOPT"
        assert verdict["chosen_capability_version_id"] == "cv-httpx-1"
        assert client.solve.call_count == 2
