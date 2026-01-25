"""Async processing queue with backpressure handling."""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Lock for thread-safe singleton initialization
_init_lock = asyncio.Lock()


def _utc_now() -> datetime:
    """Get current UTC time as naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(order=True)
class QueueItem:
    """Item in the processing queue."""

    # sort_index is used for comparison in priority queue (lower = higher priority)
    sort_index: int = field(init=False, repr=False)
    conversation_id: UUID = field(compare=False)
    priority: int = field(default=0, compare=False)  # Higher = more priority
    created_at: datetime = field(default_factory=_utc_now, compare=False)
    retries: int = field(default=0, compare=False)
    max_retries: int = field(default=3, compare=False)

    def __post_init__(self):
        # Use negative priority so higher priority items come first
        self.sort_index = -self.priority


class QueueFullError(Exception):
    """Queue is at capacity."""

    pass


class ProcessingQueue:
    """Async queue with backpressure and priority support."""

    def __init__(self, max_size: Optional[int] = None):
        self.max_size = max_size or settings.queue_max_size
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(
            maxsize=self.max_size
        )
        self._backpressure_events = 0
        self._total_enqueued = 0
        self._total_dequeued = 0

    @property
    def depth(self) -> int:
        """Current queue depth."""
        return self._queue.qsize()

    @property
    def is_full(self) -> bool:
        """Check if queue is at capacity."""
        return self._queue.full()

    @property
    def backpressure_events(self) -> int:
        """Total backpressure events."""
        return self._backpressure_events

    async def enqueue(
        self, conversation_id: UUID, priority: int = 0, retries: int = 0
    ) -> bool:
        """
        Add item to queue. Returns False if queue is full (backpressure).

        Args:
            conversation_id: The conversation to process
            priority: Higher = more priority
            retries: Current retry count (for requeued items)
        """
        if self.is_full:
            self._backpressure_events += 1
            logger.warning(
                f"Queue full, rejecting conversation {conversation_id}. "
                f"Total backpressure events: {self._backpressure_events}"
            )
            return False

        item = QueueItem(conversation_id=conversation_id, priority=priority, retries=retries)
        # QueueItem has sort_index for proper ordering in priority queue
        await self._queue.put(item)
        self._total_enqueued += 1
        logger.debug(f"Enqueued conversation {conversation_id}, queue depth: {self.depth}")
        return True

    async def dequeue(self, timeout: Optional[float] = None) -> Optional[QueueItem]:
        """
        Get next item from queue. Returns None on timeout.
        """
        try:
            if timeout:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=timeout
                )
            else:
                item = await self._queue.get()

            self._total_dequeued += 1
            return item
        except asyncio.TimeoutError:
            return None

    def task_done(self) -> None:
        """Mark a dequeued task as complete."""
        self._queue.task_done()

    async def requeue(self, item: QueueItem, delay: float = 1.0) -> bool:
        """
        Requeue an item for retry with optional delay.
        Preserves retry count across requeue operations.
        """
        new_retries = item.retries + 1
        if new_retries > item.max_retries:
            logger.warning(
                f"Max retries ({item.max_retries}) exceeded for conversation {item.conversation_id}"
            )
            return False

        new_priority = item.priority - 1  # Lower priority on retry

        if delay > 0:
            await asyncio.sleep(delay)

        # Pass retry count to preserve it across requeue
        return await self.enqueue(item.conversation_id, new_priority, retries=new_retries)

    def get_stats(self) -> dict[str, Any]:
        """Get queue statistics."""
        return {
            "depth": self.depth,
            "max_size": self.max_size,
            "is_full": self.is_full,
            "backpressure_events": self._backpressure_events,
            "total_enqueued": self._total_enqueued,
            "total_dequeued": self._total_dequeued,
        }


# Global instance
_processing_queue: Optional[ProcessingQueue] = None
_queue_init_lock = asyncio.Lock()


def get_processing_queue() -> ProcessingQueue:
    """
    Get global processing queue instance.

    Note: For truly thread-safe initialization in async context,
    use get_processing_queue_async() instead.
    """
    global _processing_queue
    if _processing_queue is None:
        _processing_queue = ProcessingQueue()
    return _processing_queue


async def get_processing_queue_async() -> ProcessingQueue:
    """Get global processing queue instance with thread-safe initialization."""
    global _processing_queue
    if _processing_queue is None:
        async with _queue_init_lock:
            # Double-check after acquiring lock
            if _processing_queue is None:
                _processing_queue = ProcessingQueue()
    return _processing_queue


def reset_processing_queue() -> None:
    """Reset the global processing queue (for testing)."""
    global _processing_queue
    _processing_queue = None
