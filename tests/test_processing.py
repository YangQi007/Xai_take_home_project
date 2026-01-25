"""Tests for processing pipeline components."""
import asyncio
import pytest
from uuid import uuid4

from app.processing.queue import ProcessingQueue, QueueItem
from app.processing.rate_limiter import AdaptiveRateLimiter
from app.processing.circuit_breaker import CircuitBreaker, CircuitState, CircuitOpenError


class TestProcessingQueue:
    """Tests for ProcessingQueue."""

    @pytest.mark.asyncio
    async def test_enqueue_dequeue(self):
        """Should enqueue and dequeue items."""
        queue = ProcessingQueue(max_size=10)
        conv_id = uuid4()

        result = await queue.enqueue(conv_id)
        assert result is True
        assert queue.depth == 1

        item = await queue.dequeue(timeout=1.0)
        assert item is not None
        assert item.conversation_id == conv_id

    @pytest.mark.asyncio
    async def test_backpressure_when_full(self):
        """Should reject items when queue is full."""
        queue = ProcessingQueue(max_size=2)

        # Fill queue
        await queue.enqueue(uuid4())
        await queue.enqueue(uuid4())

        # Should reject
        result = await queue.enqueue(uuid4())
        assert result is False
        assert queue.backpressure_events == 1

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        """Higher priority items should dequeue first."""
        queue = ProcessingQueue(max_size=10)

        id_low = uuid4()
        id_high = uuid4()

        await queue.enqueue(id_low, priority=1)
        await queue.enqueue(id_high, priority=10)

        # High priority should come first
        item1 = await queue.dequeue(timeout=1.0)
        assert item1.conversation_id == id_high

        item2 = await queue.dequeue(timeout=1.0)
        assert item2.conversation_id == id_low

    @pytest.mark.asyncio
    async def test_timeout_on_empty_queue(self):
        """Should return None on timeout for empty queue."""
        queue = ProcessingQueue(max_size=10)

        item = await queue.dequeue(timeout=0.1)
        assert item is None


class TestAdaptiveRateLimiter:
    """Tests for AdaptiveRateLimiter."""

    @pytest.mark.asyncio
    async def test_acquire_token(self):
        """Should acquire token when available."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0)

        result = await limiter.acquire(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_grow_on_success_streak(self):
        """Rate should grow after success streak."""
        limiter = AdaptiveRateLimiter(
            initial_rate=10.0,
            success_streak_threshold=3,
            grow_factor=1.5,
        )

        initial_rate = limiter.current_rate

        # Record successes
        for _ in range(3):
            limiter.record_success()

        assert limiter.current_rate > initial_rate

    def test_shrink_on_failure(self):
        """Rate should shrink on failure."""
        limiter = AdaptiveRateLimiter(
            initial_rate=10.0,
            shrink_factor=0.5,
        )

        initial_rate = limiter.current_rate
        limiter.record_failure()

        assert limiter.current_rate < initial_rate

    def test_rate_bounds(self):
        """Rate should stay within min/max bounds."""
        limiter = AdaptiveRateLimiter(
            initial_rate=10.0,
            min_rate=1.0,
            max_rate=20.0,
        )

        # Try to shrink below min
        for _ in range(10):
            limiter.record_failure()
        assert limiter.current_rate >= 1.0

        # Reset and try to grow above max
        limiter.force_rate(10.0)
        for _ in range(100):
            limiter.record_success()
        assert limiter.current_rate <= 20.0


class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    @pytest.mark.asyncio
    async def test_starts_closed(self):
        """Circuit should start in closed state."""
        circuit = CircuitBreaker()
        assert circuit.state == CircuitState.CLOSED
        assert circuit.is_closed

    @pytest.mark.asyncio
    async def test_opens_after_failures(self):
        """Circuit should open after failure threshold."""
        circuit = CircuitBreaker(failure_threshold=3)

        # Record failures
        for _ in range(3):
            await circuit.record_failure()

        assert circuit.state == CircuitState.OPEN
        assert circuit.is_open

    @pytest.mark.asyncio
    async def test_blocks_when_open(self):
        """Should block requests when open."""
        circuit = CircuitBreaker(failure_threshold=1)

        await circuit.record_failure()
        assert circuit.is_open

        can_exec = await circuit.can_execute()
        assert can_exec is False

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self):
        """Should transition to half-open after recovery timeout."""
        circuit = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,
        )

        await circuit.record_failure()
        assert circuit.is_open

        # Wait for recovery timeout
        await asyncio.sleep(0.15)

        can_exec = await circuit.can_execute()
        assert can_exec is True
        assert circuit.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_closes_after_successful_half_open(self):
        """Should close after successful requests in half-open."""
        circuit = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,
            half_open_requests=2,
        )

        await circuit.record_failure()
        await asyncio.sleep(0.15)

        # Make successful requests in half-open
        await circuit.can_execute()
        await circuit.record_success()
        await circuit.can_execute()
        await circuit.record_success()

        assert circuit.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_reopens_on_half_open_failure(self):
        """Should reopen on failure in half-open state."""
        circuit = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,
        )

        await circuit.record_failure()
        await asyncio.sleep(0.15)

        await circuit.can_execute()  # Transitions to half-open
        await circuit.record_failure()

        assert circuit.state == CircuitState.OPEN
