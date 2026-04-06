"""
Webhook notification system.

Sends HTTP notifications for:
- Canary events (registration, promotion, rollback, auto-rollback)
- Config changes (apply success/failure)
- Circuit breaker state changes
- Critical errors (provider exhaustion, queue overflow)

Webhooks are dispatched asynchronously and retry on failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class WebhookEvent(Enum):
    CANARY_REGISTERED = "canary.registered"
    CANARY_PROMOTED = "canary.promoted"
    CANARY_ROLLED_BACK = "canary.rolled_back"
    CANARY_AUTO_ROLLED_BACK = "canary.auto_rolled_back"
    CONFIG_APPLIED = "config.applied"
    CONFIG_APPLY_FAILED = "config.apply_failed"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker.open"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker.closed"
    PROVIDER_EXHAUSTED = "provider.exhausted"
    QUEUE_OVERFLOW = "queue.overflow"


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    url: str
    events: set[WebhookEvent]
    secret: str | None = None  # for HMAC signature
    max_retries: int = 3
    timeout_seconds: float = 10.0


@dataclass(slots=True)
class WebhookDelivery:
    event: WebhookEvent
    payload: dict[str, Any]
    attempts: int = 0
    last_error: str | None = None
    delivered: bool = False


class WebhookDispatcher:
    """Dispatches webhook notifications asynchronously."""

    def __init__(self, max_pending: int = 1000) -> None:
        self._webhooks: list[WebhookConfig] = []
        self._pending: deque[WebhookDelivery] = deque(maxlen=max_pending)
        self._running = False
        self._task: asyncio.Task | None = None

    def register_webhook(self, config: WebhookConfig) -> None:
        """Register a webhook endpoint."""
        self._webhooks.append(config)
        logger.info(
            "Registered webhook",
            url=config.url,
            events=[e.value for e in config.events],
        )

    def unregister_webhook(self, url: str) -> bool:
        """Remove a webhook endpoint."""
        before = len(self._webhooks)
        self._webhooks = [w for w in self._webhooks if w.url != url]
        return len(self._webhooks) < before

    def list_webhooks(self) -> list[dict[str, Any]]:
        return [
            {
                "url": w.url,
                "events": [e.value for e in w.events],
                "max_retries": w.max_retries,
                "timeout_seconds": w.timeout_seconds,
            }
            for w in self._webhooks
        ]

    def dispatch(self, event: WebhookEvent, payload: dict[str, Any]) -> None:
        """Queue a webhook delivery for async processing."""
        delivery = WebhookDelivery(event=event, payload=payload)
        self._pending.append(delivery)
        logger.debug("Queued webhook delivery", event=event.value)

    async def start(self) -> None:
        """Start the background dispatch loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("Webhook dispatcher started")

    async def stop(self) -> None:
        """Stop the background dispatch loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Webhook dispatcher stopped")

    async def _dispatch_loop(self) -> None:
        """Background loop that processes pending deliveries."""
        while self._running:
            if not self._pending:
                await asyncio.sleep(1)
                continue

            delivery = self._pending.popleft()
            await self._deliver(delivery)
            await asyncio.sleep(0.1)  # Rate limit

    async def _deliver(self, delivery: WebhookDelivery) -> None:
        """Deliver a single webhook notification."""
        for webhook in self._webhooks:
            if delivery.event not in webhook.events:
                continue

            for attempt in range(webhook.max_retries):
                delivery.attempts = attempt + 1
                try:
                    async with httpx.AsyncClient(timeout=webhook.timeout_seconds) as client:
                        response = await client.post(
                            webhook.url,
                            json=self._build_payload(delivery),
                            headers=self._build_headers(delivery, webhook),
                        )
                        if response.status_code < 400:
                            delivery.delivered = True
                            logger.info(
                                "Webhook delivered",
                                event=delivery.event.value,
                                url=webhook.url,
                                status=response.status_code,
                            )
                            break
                        else:
                            delivery.last_error = f"HTTP {response.status_code}"
                            logger.warning(
                                "Webhook delivery failed",
                                url=webhook.url,
                                status=response.status_code,
                                attempt=attempt + 1,
                            )
                except Exception as exc:
                    delivery.last_error = str(exc)
                    logger.warning(
                        "Webhook delivery error",
                        url=webhook.url,
                        error=str(exc),
                        attempt=attempt + 1,
                    )
                    if attempt < webhook.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff

    def _build_payload(self, delivery: WebhookDelivery) -> dict[str, Any]:
        return {
            "event": delivery.event.value,
            "timestamp": time.time(),
            "attempt": delivery.attempts,
            "payload": delivery.payload,
        }

    def _build_headers(
        self, delivery: WebhookDelivery, webhook: WebhookConfig
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-MoreAI-Event": delivery.event.value,
        }
        if webhook.secret:
            import hashlib
            import hmac
            body = json.dumps(self._build_payload(delivery))
            signature = hmac.new(
                webhook.secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-MoreAI-Signature"] = f"sha256={signature}"
        return headers

    def get_pending_count(self) -> int:
        return len(self._pending)


webhook_dispatcher = WebhookDispatcher()
