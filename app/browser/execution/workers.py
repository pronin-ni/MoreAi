import asyncio
import time
from collections import Counter

from app.browser.execution.errors import (
    BrowserTaskError,
    ProviderCircuitOpenError,
    QueueWaitTimeoutError,
)
from app.browser.execution.executor import BrowserProviderExecutor
from app.browser.execution.models import BrowserExecutionHealthSnapshot, BrowserJob
from app.browser.execution.queue import QueuePort
from app.browser.execution.runtime import WorkerBrowserRuntime
from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class ProviderConcurrencyLimiter:
    def __init__(self, limits: dict[str, int]):
        self._limits = {provider_id: max(1, limit) for provider_id, limit in limits.items()}
        self._semaphores = {
            provider_id: asyncio.Semaphore(limit) for provider_id, limit in self._limits.items()
        }
        self._in_flight = Counter[str]()

    async def acquire(self, provider_id: str):
        semaphore = self._semaphores.get(provider_id)
        if semaphore is None:
            return _NullAsyncContext(self._in_flight, provider_id)
        return _SemaphoreAsyncContext(semaphore, self._in_flight, provider_id)

    def snapshot(self) -> dict[str, int]:
        return dict(self._in_flight)


class ProviderHealthController:
    def __init__(self):
        self._consecutive_failures = Counter[str]()
        self._opened_until: dict[str, float] = {}

    def ensure_available(self, provider_id: str) -> None:
        now = time.monotonic()
        opened_until = self._opened_until.get(provider_id)
        if opened_until is None:
            return
        if opened_until <= now:
            self._opened_until.pop(provider_id, None)
            self._consecutive_failures[provider_id] = 0
            return
        raise ProviderCircuitOpenError(
            f"Provider {provider_id} is temporarily unavailable due to repeated browser failures",
            details={
                "provider_id": provider_id,
                "phase": "provider_circuit",
                "opened_until_monotonic": opened_until,
            },
        )

    def recommended_delay(self, provider_id: str) -> float:
        failures = self._consecutive_failures.get(provider_id, 0)
        if failures <= 0:
            return 0.0
        return min(
            settings.browser_provider_adaptive_cooldown_seconds * failures,
            settings.browser_provider_adaptive_cooldown_max_seconds,
        )

    def record_success(self, provider_id: str) -> None:
        self._consecutive_failures[provider_id] = 0
        self._opened_until.pop(provider_id, None)

    def record_failure(self, provider_id: str, retryable: bool) -> None:
        if not retryable:
            return
        self._consecutive_failures[provider_id] += 1
        if (
            self._consecutive_failures[provider_id]
            >= settings.browser_provider_circuit_failure_threshold
        ):
            self._opened_until[provider_id] = (
                time.monotonic() + settings.browser_provider_circuit_open_seconds
            )

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        now = time.monotonic()
        provider_ids = set(self._consecutive_failures.keys()) | set(self._opened_until.keys())
        return {
            provider_id: {
                "consecutive_failures": self._consecutive_failures.get(provider_id, 0),
                "is_open": self._opened_until.get(provider_id, 0.0) > now,
                "opened_until_monotonic": self._opened_until.get(provider_id, 0.0),
                "adaptive_delay_seconds": self.recommended_delay(provider_id),
            }
            for provider_id in provider_ids
        }


class _NullAsyncContext:
    def __init__(self, counter: Counter[str], provider_id: str):
        self._counter = counter
        self._provider_id = provider_id

    async def __aenter__(self):
        self._counter[self._provider_id] += 1
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self._counter[self._provider_id] -= 1
        if self._counter[self._provider_id] <= 0:
            self._counter.pop(self._provider_id, None)


class _SemaphoreAsyncContext(_NullAsyncContext):
    def __init__(self, semaphore: asyncio.Semaphore, counter: Counter[str], provider_id: str):
        super().__init__(counter, provider_id)
        self._semaphore = semaphore

    async def __aenter__(self):
        await self._semaphore.acquire()
        return await super().__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        await super().__aexit__(exc_type, exc, tb)
        self._semaphore.release()


class BrowserWorkerPool:
    def __init__(
        self,
        queue: QueuePort,
        worker_pool_size: int,
        queue_wait_timeout_seconds: int,
        provider_limits: dict[str, int],
    ):
        self.queue = queue
        self.worker_pool_size = worker_pool_size
        self.queue_wait_timeout_seconds = queue_wait_timeout_seconds
        self.provider_limiter = ProviderConcurrencyLimiter(provider_limits)
        self.provider_health = ProviderHealthController()
        self._workers: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()
        self._in_flight = 0
        self._rejected_submissions = 0
        self._timed_out_jobs = 0
        self._retry_count = 0
        self._worker_restart_count = 0
        self._active_workers = 0
        self._completed_jobs = 0
        self._failed_jobs = 0
        self._cancelled_jobs = 0
        self._drained_jobs = 0
        self._state = "stopped"

    async def start(self) -> None:
        if self._workers:
            return

        self._state = "running"
        self._shutdown.clear()
        for index in range(self.worker_pool_size):
            worker_name = f"browser-worker-{index + 1}"
            task = asyncio.create_task(self._worker_loop(worker_name), name=worker_name)
            self._workers.append(task)

    async def stop(self) -> None:
        self._state = "draining"
        self._shutdown.set()

        self.queue.close()
        try:
            await asyncio.wait_for(
                self.queue.join(), timeout=settings.browser_shutdown_grace_seconds
            )
        except TimeoutError:
            self._drain_pending_jobs()
        else:
            if self.queue.qsize() > 0:
                self._drain_pending_jobs()

        self._state = "stopping"
        for task in self._workers:
            task.cancel()

        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        self._active_workers = 0
        self._state = "stopped"

    def increment_rejected_submissions(self) -> None:
        self._rejected_submissions += 1

    def health_snapshot(self) -> BrowserExecutionHealthSnapshot:
        return BrowserExecutionHealthSnapshot(
            queue_size=self.queue.qsize(),
            queue_capacity=self.queue.maxsize(),
            in_flight=self._in_flight,
            active_workers=self._active_workers,
            worker_pool_size=self.worker_pool_size,
            provider_in_flight=self.provider_limiter.snapshot(),
            rejected_submissions=self._rejected_submissions,
            timed_out_jobs=self._timed_out_jobs,
            retry_count=self._retry_count,
            worker_restart_count=self._worker_restart_count,
            completed_jobs=self._completed_jobs,
            failed_jobs=self._failed_jobs,
            cancelled_jobs=self._cancelled_jobs,
            drained_jobs=self._drained_jobs,
            queue_oldest_age_seconds=self.queue.oldest_age_seconds(),
            provider_circuit_state=self.provider_health.snapshot(),
            state=self._state,
        )

    async def _worker_loop(self, worker_name: str) -> None:
        runtime = WorkerBrowserRuntime(worker_name=worker_name)
        executor = BrowserProviderExecutor(
            runtime=runtime,
            on_runtime_restart=self._increment_worker_restart_count,
        )

        try:
            await runtime.start()
            self._active_workers += 1
            while True:
                if self._shutdown.is_set() and self.queue.qsize() == 0:
                    break
                try:
                    job = await asyncio.wait_for(self.queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                try:
                    await self._run_job(executor, job)
                finally:
                    self.queue.task_done()
        finally:
            self._active_workers = max(0, self._active_workers - 1)
            await runtime.stop()

    def _increment_worker_restart_count(self) -> None:
        self._worker_restart_count += 1

    async def _run_job(self, executor: BrowserProviderExecutor, job: BrowserJob) -> None:
        request = job.request

        if job.is_cancelled():
            if not job.result_future.done():
                job.result_future.cancel()
            return

        queue_wait_seconds = asyncio.get_running_loop().time() - job.enqueued_at
        if queue_wait_seconds > request.queue_wait_timeout_seconds:
            self._timed_out_jobs += 1
            if not job.result_future.done():
                job.result_future.set_exception(
                    QueueWaitTimeoutError(
                        "Browser task timed out while waiting in queue",
                        details={
                            "task_id": request.task_id,
                            "provider_id": request.provider_id,
                            "phase": "queue",
                        },
                    ).to_api_error()
                )
            return

        limiter = await self.provider_limiter.acquire(request.provider_id)

        try:
            self.provider_health.ensure_available(request.provider_id)
            delay = self.provider_health.recommended_delay(request.provider_id)
            if delay > 0:
                await asyncio.sleep(delay)
            async with limiter:
                self._in_flight += 1
                result = await executor.execute(job)
                self._retry_count += result.retry_count
                self.provider_health.record_success(request.provider_id)
                self._completed_jobs += 1
                if not job.result_future.done():
                    job.result_future.set_result(result)
        except asyncio.CancelledError:
            self._cancelled_jobs += 1
            if not job.result_future.done():
                job.result_future.cancel()
        except BrowserTaskError as exc:
            self._failed_jobs += 1
            self.provider_health.record_failure(request.provider_id, retryable=exc.retryable)
            if not job.result_future.done():
                job.result_future.set_exception(exc.to_api_error())
        except Exception as exc:
            self._failed_jobs += 1
            retryable = False
            if isinstance(exc, ServiceUnavailableError):
                retryable = bool(exc.details.get("retryable", False))
            self.provider_health.record_failure(request.provider_id, retryable=retryable)
            if not job.result_future.done():
                job.result_future.set_exception(exc)
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def _drain_pending_jobs(self) -> None:
        drained_jobs = self.queue.drain()
        for job in drained_jobs:
            self._drained_jobs += 1
            if not job.result_future.done():
                job.result_future.set_exception(
                    ServiceUnavailableError(
                        "Browser task queue was drained during shutdown",
                        details={
                            "task_id": job.request.task_id,
                            "provider_id": job.request.provider_id,
                            "phase": "shutdown",
                            "failure_kind": "shutdown_drain",
                            "retryable": True,
                        },
                    )
                )
            self.queue.task_done()
