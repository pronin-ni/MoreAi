import asyncio
from abc import ABC, abstractmethod

from app.browser.execution.models import BrowserJob


class QueuePort(ABC):
    @abstractmethod
    async def put(self, job: BrowserJob, timeout: float | None = None) -> None:
        pass

    @abstractmethod
    async def get(self) -> BrowserJob:
        pass

    @abstractmethod
    def task_done(self) -> None:
        pass

    @abstractmethod
    def qsize(self) -> int:
        pass

    @abstractmethod
    def maxsize(self) -> int:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def closed(self) -> bool:
        pass

    @abstractmethod
    async def join(self) -> None:
        pass

    @abstractmethod
    def drain(self) -> list[BrowserJob]:
        pass

    @abstractmethod
    def oldest_age_seconds(self) -> float:
        pass


class InMemoryBrowserTaskQueue(QueuePort):
    def __init__(self, max_size: int):
        self._queue: asyncio.PriorityQueue[tuple[int, int, BrowserJob]] = asyncio.PriorityQueue(
            maxsize=max_size
        )
        self._closed = False

    async def put(self, job: BrowserJob, timeout: float | None = None) -> None:
        if self._closed:
            raise RuntimeError("Browser task queue is closed")

        item = (job.request.priority, job.sequence, job)
        if timeout is None:
            await self._queue.put(item)
            return

        await asyncio.wait_for(self._queue.put(item), timeout=timeout)

    async def get(self) -> BrowserJob:
        _priority, _sequence, job = await self._queue.get()
        return job

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()

    def maxsize(self) -> int:
        return self._queue.maxsize

    def close(self) -> None:
        self._closed = True

    def closed(self) -> bool:
        return self._closed

    async def join(self) -> None:
        await self._queue.join()

    def drain(self) -> list[BrowserJob]:
        drained: list[BrowserJob] = []
        while True:
            try:
                _priority, _sequence, job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            drained.append(job)
        return drained

    def oldest_age_seconds(self) -> float:
        if not self._queue._queue:  # noqa: SLF001
            return 0.0

        _priority, _sequence, job = self._queue._queue[0]  # noqa: SLF001
        return max(0.0, asyncio.get_running_loop().time() - job.enqueued_at)
