"""
FAIR Free AI Router client for CIP.

Wraps the embeddable `fair` module to route AI inference through free,
quality-verified models (Gemini, Groq, OpenRouter free tier, Ollama).
FAIR is an optional dependency: install with `pip install "cip[fair]"`.

Configuration via environment variables (at least one provider required):
  GEMINI_API_KEY       — Google Gemini free tier
  GROQ_API_KEY         — Groq free plan
  OPENROUTER_API_KEY   — OpenRouter, free models only
  OLLAMA_ENABLED=1     — local Ollama (OLLAMA_URL defaults to 127.0.0.1:11434)
  FAIR_CLIENT_ID       — client identifier (default: cip)
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")

# Groq, OpenRouter and Ollama adapters reject output budgets above 4096.
MAX_OUTPUT_TOKENS = 4096


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


def _run(coro: Awaitable[T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Called from inside a running loop (e.g. a sync FastMCP tool): hop to a worker thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class FairClient:
    def __init__(
        self,
        client_id: str | None = None,
        quality_level: str = "standard",
        timeout: float | None = None,
        providers: list[tuple[Any, Any]] | None = None,
    ):
        self.client_id = client_id or os.getenv("FAIR_CLIENT_ID", "cip")
        self.quality_level = quality_level
        self.timeout = timeout
        self._providers = providers

    def _build(self):
        from fair import FAIR

        kwargs: dict[str, Any] = {
            "providers": self._providers,
            "quality_level": self.quality_level,
        }
        if self.timeout is not None:
            kwargs["timeout_seconds"] = self.timeout
        return FAIR(**kwargs)

    def solve(
        self,
        task: str,
        *,
        task_type: str | None = None,
        quality_level: str | None = None,
        expected_schema: dict | None = None,
        cross_check: bool = False,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> FairResponse:
        async def _solve():
            fair = self._build()
            try:
                return await fair.solve(
                    task,
                    task_type=task_type,
                    quality_level=quality_level,
                    expected_schema=expected_schema,
                    cross_check_required=cross_check,
                    max_output_tokens=max_output_tokens,
                    client_id=self.client_id,
                )
            finally:
                await fair.close()

        resp = _run(_solve())
        return FairResponse(
            request_id=resp.request_id,
            status=resp.status,
            reason_code=resp.reason_code,
            output=resp.output,
            verification_state=resp.verification_state,
            best_quality_score=resp.best_quality_score,
            provider_id=resp.provider_id,
            model_id=resp.model_id,
            attempts=[a.model_dump(mode="json") for a in resp.attempts],
            raw=resp.model_dump(mode="json"),
        )

    def status(self) -> dict[str, Any]:
        base = {"client_id": self.client_id, "providers": []}
        try:
            fair = self._build()
        except ImportError:
            return {
                **base,
                "available": False,
                "error": "fair package not installed — pip install \"cip[fair]\"",
            }
        except Exception as exc:
            return {**base, "available": False, "error": str(exc)}
        return {**base, "available": True, "providers": fair.providers(), "error": None}

    def is_available(self) -> bool:
        return self.status()["available"]
