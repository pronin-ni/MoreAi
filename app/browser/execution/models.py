import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BrowserExecutionRequest:
    task_id: str
    request_id: str
    provider_id: str
    canonical_model_id: str
    message: str
    created_at: float = field(default_factory=time.monotonic)
    priority: int = 100
    queue_wait_timeout_seconds: int = 30
    execution_timeout_seconds: int = 120
    max_retries: int = 1


@dataclass(slots=True)
class BrowserJobResult:
    content: str
    started_at: float
    finished_at: float
    queue_wait_seconds: float
    execution_seconds: float
    retry_count: int


@dataclass(slots=True)
class BrowserJob:
    request: BrowserExecutionRequest
    result_future: asyncio.Future[BrowserJobResult]
    sequence: int
    # Use event loop clock for consistent time comparison with _run_job
    enqueued_at: float = field(default_factory=lambda: asyncio.get_running_loop().time())
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float | None = None
    retry_count: int = 0

    def mark_cancelled(self) -> None:
        self.cancelled.set()

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set() or self.result_future.cancelled()


@dataclass(slots=True)
class BrowserExecutionHealthSnapshot:
    queue_size: int
    queue_capacity: int
    in_flight: int
    active_workers: int
    worker_pool_size: int
    provider_in_flight: dict[str, int]
    rejected_submissions: int
    timed_out_jobs: int
    retry_count: int
    worker_restart_count: int
    completed_jobs: int
    failed_jobs: int
    cancelled_jobs: int
    drained_jobs: int
    queue_oldest_age_seconds: float
    provider_circuit_state: dict[str, dict[str, Any]]
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_size": self.queue_size,
            "queue_capacity": self.queue_capacity,
            "in_flight": self.in_flight,
            "active_workers": self.active_workers,
            "worker_pool_size": self.worker_pool_size,
            "provider_in_flight": self.provider_in_flight,
            "rejected_submissions": self.rejected_submissions,
            "timed_out_jobs": self.timed_out_jobs,
            "retry_count": self.retry_count,
            "worker_restart_count": self.worker_restart_count,
            "completed_jobs": self.completed_jobs,
            "failed_jobs": self.failed_jobs,
            "cancelled_jobs": self.cancelled_jobs,
            "drained_jobs": self.drained_jobs,
            "queue_oldest_age_seconds": self.queue_oldest_age_seconds,
            "provider_circuit_state": self.provider_circuit_state,
            "state": self.state,
        }
