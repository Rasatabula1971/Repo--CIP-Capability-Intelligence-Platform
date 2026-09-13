"""
FAIR Free AI Router client for CIP.

Wraps FAIR's POST /v1/solve endpoint to route AI inference through
free models (Groq, OpenRouter free tier, Ollama).

Configuration via environment variables:
  FAIR_API_URL     — FAIR service URL (default: http://127.0.0.1:8000)
  FAIR_CLIENT_KEY  — API key for authentication
  FAIR_CLIENT_ID   — client identifier (default: cip)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FairResponse:
    request_id: str
    status: str
    reason_code: str
    output: str | None
    verification_state: str
    best_quality_score: float | None
    provider_id: str | None
    model_id: str | None
    attempts: list[dict[str, Any]]
    raw: dict[str, Any] = field(repr=False)

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"

    @property
    def escalated(self) -> bool:
        return self.status == "ESCALATION_REQUIRED"

    def output_json(self) -> dict[str, Any] | None:
        if not self.output:
            return None
        text = self.output.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)


class FairClient:
    def __init__(
        self,
        api_url: str | None = None,
        client_key: str | None = None,
        client_id: str | None = None,
        timeout: float = 120.0,
    ):
        self.api_url = (api_url or os.getenv("FAIR_API_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.client_key = client_key or os.getenv("FAIR_CLIENT_KEY", "")
        self.client_id = client_id or os.getenv("FAIR_CLIENT_ID", "cip")
        self.timeout = timeout

    def solve(
        self,
        task: str,
        *,
        task_type: str | None = None,
        quality_level: str = "standard",
        expected_schema: dict | None = None,
        cross_check: bool = False,
    ) -> FairResponse:
        payload: dict[str, Any] = {
            "client_id": self.client_id,
            "task": task,
            "quality_level": quality_level,
        }
        if task_type:
            payload["task_type"] = task_type
        if expected_schema:
            payload["expected_schema"] = expected_schema
        if cross_check:
            payload["cross_check_required"] = True

        resp = httpx.post(
            f"{self.api_url}/v1/solve",
            json=payload,
            headers={"X-API-Key": self.client_key},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        return FairResponse(
            request_id=data.get("request_id", ""),
            status=data.get("status", "FAILED"),
            reason_code=data.get("reason_code", "UNKNOWN"),
            output=data.get("output"),
            verification_state=data.get("verification_state", "UNVERIFIED"),
            best_quality_score=data.get("best_quality_score"),
            provider_id=data.get("provider_id"),
            model_id=data.get("model_id"),
            attempts=data.get("attempts", []),
            raw=data,
        )

    def is_available(self) -> bool:
        try:
            resp = httpx.get(
                f"{self.api_url}/healthz",
                timeout=5.0,
            )
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
