"""Background workers for processing conversations."""
import asyncio
import logging
import time
from typing import List, Optional

from app.config import get_settings
from app.database import get_db_context
from app.processing.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    get_circuit_breaker,
)
from app.processing.queue import ProcessingQueue, QueueItem, get_processing_queue
from app.processing.rate_limiter import AdaptiveRateLimiter, get_rate_limiter
from app.services.analysis import AnalysisService, get_analysis_service
from app.services.grok_client import GrokRateLimitError

logger = logging.getLogger(__name__)
settings = get_settings()


class Worker:
    """Single worker that processes conversations from the queue."""

    def __init__(
        self,
        worker_id: int,
        queue: ProcessingQueue,
        rate_limiter: AdaptiveRateLimiter,
        circuit_breaker: CircuitBreaker,
        analysis_service: AnalysisService,
    ):
        self.worker_id = worker_id
        self.queue = queue
        self.rate_limiter = rate_limiter
        self.circuit_breaker = circuit_breaker
        self.analysis_service = analysis_service
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Stats
        self._processed = 0
        self._failures = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the worker."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"Worker {self.worker_id} started")

    async def stop(self) -> None:
        """Stop the worker gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"Worker {self.worker_id} stopped")

    async def _run(self) -> None:
        """Main worker loop."""
        while self._running:
            item = None
            try:
                # Get item from queue with timeout
                item = await self.queue.dequeue(timeout=1.0)
                if item is None:
                    continue

                await self._process_item(item)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}")
                await asyncio.sleep(1.0)  # Brief pause on error
            finally:
                # Always mark task as done if we dequeued an item
                if item is not None:
                    try:
                        self.queue.task_done()
                    except ValueError:
                        # task_done called too many times - ignore
                        pass

    async def _process_item(self, item: QueueItem) -> None:
        """Process a single queue item."""
        conversation_id = item.conversation_id
        logger.debug(f"Worker {self.worker_id} processing {conversation_id}")

        start_time = time.monotonic()

        try:
            # Check circuit breaker
            if not await self.circuit_breaker.can_execute():
                retry_after = self.circuit_breaker.get_retry_after()
                logger.warning(
                    f"Circuit open, requeueing {conversation_id}. "
                    f"Retry after: {retry_after:.1f}s"
                )
                await self.queue.requeue(item, delay=retry_after)
                return

            # Acquire rate limit token
            if not await self.rate_limiter.acquire(timeout=30.0):
                logger.warning(f"Rate limit timeout, requeueing {conversation_id}")
                await self.queue.requeue(item, delay=1.0)
                return

            # Process the conversation
            async with get_db_context() as db:
                insight = await self.analysis_service.analyze_conversation(
                    db, conversation_id
                )

            # Record success
            elapsed = time.monotonic() - start_time
            await self.circuit_breaker.record_success()
            self.rate_limiter.record_success()
            self.rate_limiter.record_latency_spike(elapsed)

            self._processed += 1
            logger.info(
                f"Worker {self.worker_id} processed {conversation_id} "
                f"in {elapsed:.2f}s, insight: {insight is not None}"
            )

        except GrokRateLimitError as e:
            # Handle rate limit from Grok API
            self._failures += 1
            self.rate_limiter.record_failure(severity=0.8)

            delay = e.retry_after or 10.0
            logger.warning(
                f"Grok rate limited, requeueing {conversation_id}. "
                f"Retry after: {delay:.1f}s"
            )
            await self.queue.requeue(item, delay=delay)

        except CircuitOpenError as e:
            # Circuit breaker tripped
            logger.warning(f"Circuit open, requeueing {conversation_id}")
            await self.queue.requeue(item, delay=e.retry_after)

        except Exception as e:
            # General failure
            self._failures += 1
            await self.circuit_breaker.record_failure()
            self.rate_limiter.record_failure()

            logger.error(
                f"Worker {self.worker_id} failed processing {conversation_id}: {e}"
            )

            # Requeue if retries remaining
            if item.retries < item.max_retries:
                delay = 2 ** item.retries  # Exponential backoff
                await self.queue.requeue(item, delay=delay)

    def get_stats(self) -> dict:
        """Get worker statistics."""
        return {
            "worker_id": self.worker_id,
            "running": self._running,
            "processed": self._processed,
            "failures": self._failures,
        }


class WorkerPool:
    """Pool of workers for parallel processing."""

    def __init__(
        self,
        num_workers: Optional[int] = None,
        queue: Optional[ProcessingQueue] = None,
        rate_limiter: Optional[AdaptiveRateLimiter] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        analysis_service: Optional[AnalysisService] = None,
    ):
        self.num_workers = num_workers or settings.worker_count
        self.queue = queue or get_processing_queue()
        self.rate_limiter = rate_limiter or get_rate_limiter()
        self.circuit_breaker = circuit_breaker or get_circuit_breaker()
        self.analysis_service = analysis_service or get_analysis_service()

        self._workers: List[Worker] = []
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start all workers."""
        if self._running:
            return

        self._running = True
        self._workers = [
            Worker(
                worker_id=i,
                queue=self.queue,
                rate_limiter=self.rate_limiter,
                circuit_breaker=self.circuit_breaker,
                analysis_service=self.analysis_service,
            )
            for i in range(self.num_workers)
        ]

        for worker in self._workers:
            await worker.start()

        logger.info(f"Worker pool started with {self.num_workers} workers")

    async def stop(self) -> None:
        """Stop all workers."""
        self._running = False
        for worker in self._workers:
            await worker.stop()
        self._workers = []
        logger.info("Worker pool stopped")

    def get_stats(self) -> dict:
        """Get pool statistics."""
        return {
            "running": self._running,
            "num_workers": self.num_workers,
            "workers": [w.get_stats() for w in self._workers],
            "queue": self.queue.get_stats(),
            "rate_limiter": {
                "current_rate": self.rate_limiter.current_rate,
                "stats": self.rate_limiter.get_stats().__dict__,
            },
            "circuit_breaker": self.circuit_breaker.get_stats().__dict__,
        }


# Global instance
_worker_pool: Optional[WorkerPool] = None


def get_worker_pool() -> WorkerPool:
    """Get global worker pool instance."""
    global _worker_pool
    if _worker_pool is None:
        _worker_pool = WorkerPool()
    return _worker_pool


async def start_worker_pool() -> None:
    """Start the global worker pool."""
    pool = get_worker_pool()
    await pool.start()


async def stop_worker_pool() -> None:
    """Stop the global worker pool."""
    global _worker_pool
    if _worker_pool:
        await _worker_pool.stop()
        _worker_pool = None
