"""
Admin sandbox service — execute test prompts for diagnostics and comparison.

Provides:
- Single prompt execution against a specific model/provider
- Multi-provider comparison (same prompt, multiple targets)
- Detailed result info including latency, route info, and errors
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.registry.unified import unified_registry
from app.schemas.openai import ChatCompletionRequest
from app.services.chat_proxy_service import service as chat_proxy_service

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Result of a single sandbox execution."""

    model_id: str
    provider_id: str
    transport: str
    status: str  # "success", "error"
    content: str | None = None
    latency_seconds: float = 0.0
    error: str | None = None
    error_type: str | None = None
    route_info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompareResult:
    """Result of a multi-provider comparison."""

    prompt: str
    model_id: str
    results: list[SandboxResult]
    total_duration_seconds: float = 0.0
    fastest_result: SandboxResult | None = None
    successful_count: int = 0
    failed_count: int = 0


class SandboxService:
    """Admin sandbox for test prompt execution."""

    async def run_prompt(
        self,
        *,
        prompt: str,
        model_id: str,
        provider_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> SandboxResult:
        """Execute a test prompt against a specific model.

        Parameters
        ----------
        prompt : user message text
        model_id : target model (e.g. "browser/yandex", "gpt-4o")
        provider_id : optional, force a specific provider
        max_tokens : response token limit
        temperature : sampling temperature

        Returns
        -------
        SandboxResult with content, latency, and route info.
        """
        start = time.monotonic()
        request_id = f"sandbox_{int(start * 1000)}"

        # Validate model exists
        try:
            resolved = unified_registry.resolve_model(model_id)
        except Exception as exc:
            return SandboxResult(
                model_id=model_id,
                provider_id=provider_id or "unknown",
                transport="unknown",
                status="error",
                error=str(exc),
                error_type=type(exc).__name__,
                latency_seconds=time.monotonic() - start,
            )

        # Build request
        request = ChatCompletionRequest(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        try:
            response = await chat_proxy_service.process_completion(request, request_id)

            latency = time.monotonic() - start
            content = ""
            if response.choices:
                choice = response.choices[0]
                if choice.message and choice.message.content:
                    content = choice.message.content

            return SandboxResult(
                model_id=model_id,
                provider_id=getattr(response, "_provider", resolved.provider_id or "unknown"),
                transport=getattr(response, "_transport", resolved.transport or "unknown"),
                status="success",
                content=content,
                latency_seconds=round(latency, 3),
                route_info={
                    "model": model_id,
                    "resolved_model": resolved.canonical_id,
                    "provider_id": getattr(response, "_provider", resolved.provider_id),
                    "transport": getattr(response, "_transport", resolved.transport),
                },
            )

        except Exception as exc:
            latency = time.monotonic() - start
            logger.warning(
                "Sandbox prompt failed",
                model=model_id,
                provider=provider_id,
                error=str(exc),
                latency=round(latency, 3),
            )
            return SandboxResult(
                model_id=model_id,
                provider_id=provider_id or "unknown",
                transport="unknown",
                status="error",
                error=str(exc),
                error_type=type(exc).__name__,
                latency_seconds=round(latency, 3),
            )

    async def compare_providers(
        self,
        *,
        prompt: str,
        model_ids: list[str],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        parallel: bool = True,
    ) -> CompareResult:
        """Execute the same prompt against multiple models/providers.

        Parameters
        ----------
        prompt : user message text
        model_ids : list of target models to compare
        max_tokens : response token limit
        temperature : sampling temperature
        parallel : if True, run comparisons concurrently

        Returns
        -------
        CompareResult with per-model results and summary stats.
        """
        import asyncio

        total_start = time.monotonic()
        results: list[SandboxResult] = []

        async def _run_one(model_id: str) -> SandboxResult:
            return await self.run_prompt(
                prompt=prompt,
                model_id=model_id,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        if parallel and len(model_ids) > 1:
            tasks = [_run_one(mid) for mid in model_ids]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(gathered):
                if isinstance(result, Exception):
                    results.append(SandboxResult(
                        model_id=model_ids[i],
                        provider_id="unknown",
                        transport="unknown",
                        status="error",
                        error=str(result),
                        error_type=type(result).__name__,
                        latency_seconds=0.0,
                    ))
                else:
                    results.append(result)
        else:
            for mid in model_ids:
                result = await _run_one(mid)
                results.append(result)

        total_duration = time.monotonic() - total_start
        successful = [r for r in results if r.status == "success"]
        failed = [r for r in results if r.status == "error"]
        fastest = min(successful, key=lambda r: r.latency_seconds) if successful else None

        return CompareResult(
            prompt=prompt,
            model_id=model_ids[0] if model_ids else "unknown",
            results=results,
            total_duration_seconds=round(total_duration, 3),
            fastest_result=fastest,
            successful_count=len(successful),
            failed_count=len(failed),
        )

    def get_available_models(self) -> list[dict[str, Any]]:
        """List all models available for sandbox testing."""
        return unified_registry.list_models()


sandbox_service = SandboxService()
