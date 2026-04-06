import asyncio

import pytest

from app.browser.execution.dispatcher import BrowserTaskDispatcher
from app.browser.execution.errors import RetryableBrowserTaskError
from app.browser.execution.models import BrowserExecutionRequest, BrowserJob
from app.browser.execution.workers import BrowserWorkerPool
from app.core.errors import ServiceUnavailableError


class _QueueStub:
    def __init__(self):
        self._size = 0

    async def put(self, job, timeout=None):
        raise TimeoutError()

    async def get(self):
        raise NotImplementedError

    def task_done(self):
        return None

    def qsize(self):
        return self._size

    def maxsize(self):
        return 1

    def close(self):
        return None

    def closed(self):
        return False

    async def join(self):
        return None

    def drain(self):
        return []

    def oldest_age_seconds(self):
        return 0.0


class _PoolStub:
    def __init__(self):
        self.rejected = 0

    async def start(self):
        return None

    async def stop(self):
        return None

    def increment_rejected_submissions(self):
        self.rejected += 1

    def health_snapshot(self):
        class Snapshot:
            def to_dict(self):
                return {"state": "running"}

        return Snapshot()


class _QueueMetricsStub:
    def __init__(self):
        self._jobs = []

    def qsize(self):
        return len(self._jobs)

    def maxsize(self):
        return 1

    def task_done(self):
        return None

    def close(self):
        return None

    def closed(self):
        return False

    async def join(self):
        return None

    def drain(self):
        drained = list(self._jobs)
        self._jobs.clear()
        return drained

    def oldest_age_seconds(self):
        return 0.0


class TestBrowserTaskDispatcher:
    @pytest.mark.asyncio
    async def test_submit_returns_service_unavailable_when_enqueue_times_out(self):
        dispatcher = BrowserTaskDispatcher()
        dispatcher._queue = _QueueStub()
        dispatcher._pool = _PoolStub()
        dispatcher._started = True

        with pytest.raises(ServiceUnavailableError):
            await dispatcher.submit_and_wait(
                request_id="req-1",
                provider_id="kimi",
                canonical_model_id="browser/kimi",
                message="Hello",
            )

        assert dispatcher._pool.rejected == 1


class TestBrowserWorkerPool:
    @pytest.mark.asyncio
    async def test_run_job_returns_queue_timeout_for_stale_job(self):
        pool = BrowserWorkerPool(
            queue=_QueueMetricsStub(),
            worker_pool_size=1,
            queue_wait_timeout_seconds=1,
            provider_limits={},
        )
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = BrowserExecutionRequest(
            task_id="task-1",
            request_id="req-1",
            provider_id="kimi",
            canonical_model_id="browser/kimi",
            message="hello",
            queue_wait_timeout_seconds=0,
        )
        job = BrowserJob(request=request, result_future=future, sequence=1, enqueued_at=0)

        class ExecutorStub:
            async def execute(self, _job):
                raise AssertionError("Executor must not run for stale queue item")

        await pool._run_job(ExecutorStub(), job)

        with pytest.raises(ServiceUnavailableError):
            future.result()

    @pytest.mark.asyncio
    async def test_retryable_failures_open_provider_circuit(self):
        pool = BrowserWorkerPool(
            queue=_QueueMetricsStub(),
            worker_pool_size=1,
            queue_wait_timeout_seconds=1,
            provider_limits={},
        )

        class ExecutorStub:
            async def execute(self, job):
                raise RetryableBrowserTaskError(
                    "Transient browser issue",
                    details={
                        "task_id": job.request.task_id,
                        "provider_id": job.request.provider_id,
                    },
                )

        for index in range(3):
            future = asyncio.get_running_loop().create_future()
            request = BrowserExecutionRequest(
                task_id=f"task-{index}",
                request_id=f"req-{index}",
                provider_id="kimi",
                canonical_model_id="browser/kimi",
                message="hello",
            )
            job = BrowserJob(request=request, result_future=future, sequence=index)
            await pool._run_job(ExecutorStub(), job)
            with pytest.raises(ServiceUnavailableError):
                future.result()

        snapshot = pool.health_snapshot().to_dict()
        assert snapshot["provider_circuit_state"]["kimi"]["is_open"] is True

        future = asyncio.get_running_loop().create_future()
        request = BrowserExecutionRequest(
            task_id="task-open",
            request_id="req-open",
            provider_id="kimi",
            canonical_model_id="browser/kimi",
            message="hello",
        )
        job = BrowserJob(request=request, result_future=future, sequence=100)
        await pool._run_job(ExecutorStub(), job)
        with pytest.raises(ServiceUnavailableError) as exc_info:
            future.result()
        assert exc_info.value.details["failure_kind"] == "provider_circuit_open"

    @pytest.mark.asyncio
    async def test_stop_drains_pending_jobs(self):
        queue = _QueueMetricsStub()
        pool = BrowserWorkerPool(
            queue=queue,
            worker_pool_size=1,
            queue_wait_timeout_seconds=1,
            provider_limits={},
        )
        future = asyncio.get_running_loop().create_future()
        request = BrowserExecutionRequest(
            task_id="task-drain",
            request_id="req-drain",
            provider_id="kimi",
            canonical_model_id="browser/kimi",
            message="hello",
        )
        queue._jobs.append(BrowserJob(request=request, result_future=future, sequence=1))

        await pool.stop()

        with pytest.raises(ServiceUnavailableError) as exc_info:
            future.result()
        assert exc_info.value.details["failure_kind"] == "shutdown_drain"
