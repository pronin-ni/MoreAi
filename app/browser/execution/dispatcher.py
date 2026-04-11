import asyncio
from itertools import count
from uuid import uuid4

from app.browser.execution.models import BrowserExecutionRequest, BrowserJob, BrowserJobResult
from app.browser.execution.queue import InMemoryBrowserTaskQueue
from app.browser.execution.workers import BrowserWorkerPool
from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BrowserTaskDispatcher:
    def __init__(self):
        self._queue = InMemoryBrowserTaskQueue(max_size=settings.browser_queue_max_size)
        self._pool = BrowserWorkerPool(
            queue=self._queue,
            worker_pool_size=settings.browser_pool_size,
            queue_wait_timeout_seconds=settings.browser_queue_wait_timeout_seconds,
            provider_limits=settings.browser_provider_concurrency_limits,
        )
        self._sequence = count()
        self._started = False
        self._closing = False

    async def initialize(self) -> None:
        if self._started:
            return
        self._closing = False
        await self._pool.start()
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._closing = True
        await self._pool.stop()
        self._started = False

    async def submit_and_wait(
        self,
        request_id: str,
        provider_id: str,
        canonical_model_id: str,
        message: str,
    ) -> BrowserJobResult:
        if not self._started or self._closing or self._queue.closed():
            raise ServiceUnavailableError("Browser task dispatcher is not available")

        request = BrowserExecutionRequest(
            task_id=str(uuid4()),
            request_id=request_id,
            provider_id=provider_id,
            canonical_model_id=canonical_model_id,
            message=message,
            queue_wait_timeout_seconds=settings.browser_queue_wait_timeout_seconds,
            execution_timeout_seconds=settings.browser_task_execution_timeout_seconds,
            max_retries=settings.browser_max_retries,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[BrowserJobResult] = loop.create_future()
        job = BrowserJob(request=request, result_future=future, sequence=next(self._sequence))

        try:
            await self._queue.put(job, timeout=settings.browser_enqueue_timeout_seconds)
        except TimeoutError as exc:
            self._pool.increment_rejected_submissions()
            raise ServiceUnavailableError(
                "Browser queue is full; try again later",
                details={
                    "provider_id": provider_id,
                    "model": canonical_model_id,
                    "phase": "enqueue",
                    "failure_kind": "enqueue_timeout",
                    "retryable": True,
                },
            ) from exc
        except RuntimeError as exc:
            raise ServiceUnavailableError(
                "Browser task dispatcher is shutting down",
                details={
                    "provider_id": provider_id,
                    "model": canonical_model_id,
                    "phase": "enqueue",
                    "failure_kind": "dispatcher_unavailable",
                    "retryable": True,
                },
            ) from exc

        try:
            return await future
        except asyncio.CancelledError:
            job.mark_cancelled()
            future.cancel()
            raise

    def diagnostics(self) -> dict:
        return self._pool.health_snapshot().to_dict()

    def get_health_snapshot(self):
        """Return health snapshot object for health checks."""
        return self._pool.health_snapshot()


browser_dispatcher = BrowserTaskDispatcher()
