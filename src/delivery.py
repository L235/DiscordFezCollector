"""Independent bounded FIFO queues for best-effort feed destinations."""
import asyncio
from dataclasses import dataclass

from src.logging_setup import logger


@dataclass
class _Worker:
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    dropped: int = 0


class DestinationDispatcher:
    def __init__(self, deliver, *, max_queue=100, idle_timeout=300):
        if max_queue < 1 or idle_timeout <= 0:
            raise ValueError('Delivery queue size and idle timeout must be positive')
        self.deliver = deliver
        self.max_queue = max_queue
        self.idle_timeout = idle_timeout
        self.workers = {}
        self.closed = False

    def submit(self, key, message):
        """Never block routing. Drop newest on a full destination and log losses."""
        if self.closed:
            return False
        worker = self.workers.get(key)
        if worker is None:
            worker = _Worker(asyncio.Queue(maxsize=self.max_queue))
            self.workers[key] = worker
            worker.task = asyncio.create_task(self._run(key, worker))
        try:
            worker.queue.put_nowait(message)
        except asyncio.QueueFull:
            worker.dropped += 1
            if worker.dropped == 1 or worker.dropped % 100 == 0:
                logger.warning('Delivery queue full for %s; dropped %d newest event(s)', key, worker.dropped)
            return False
        return True

    async def _run(self, key, worker):
        try:
            while True:
                try:
                    message = await asyncio.wait_for(worker.queue.get(), self.idle_timeout)
                except asyncio.TimeoutError:
                    # A producer can enqueue as the timeout callback fires.
                    # Keep the existing queue if it gained work before cleanup.
                    if not worker.queue.empty():
                        continue
                    return
                try:
                    await self.deliver(key, message)
                except Exception:
                    logger.exception('Delivery failed for %s; continuing destination queue', key)
                finally:
                    worker.queue.task_done()
        finally:
            if self.workers.get(key) is worker:
                self.workers.pop(key, None)

    async def close(self):
        """Cancel owned workers when the stream worker shuts down."""
        self.closed = True
        tasks = [worker.task for worker in self.workers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
