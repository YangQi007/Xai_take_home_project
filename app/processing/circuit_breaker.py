"""Circuit breaker for Grok API failure recovery."""
import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, requests allowed
    OPEN = "open"  # Failures detected, requests blocked
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitOpenError(Exception):
    """Raised when circuit is open and request is blocked."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Circuit open. Retry after {retry_after:.1f}s")


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker."""

    state: CircuitState
    failure_count: int
    success_count: int
    last_failure_time: Optional[float]
    last_state_change: float
    total_calls: int
    total_failures: int
    total_blocked: int


class CircuitBreaker:
    """
    Circuit breaker pattern implementation.

    States:
    - CLOSED: Normal operation, tracking failures
    - OPEN: Too many failures, blocking requests
    - HALF_OPEN: Testing recovery with limited requests
    """

    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None,
        half_open_requests: Optional[int] = None,
    ):
        self.failure_threshold = failure_threshold or settings.circuit_breaker_failure_threshold
        self.recovery_timeout = recovery_timeout or settings.circuit_breaker_recovery_timeout
        self.half_open_requests = half_open_requests or settings.circuit_breaker_half_open_requests

        # State
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_count = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change = time.monotonic()
        self._lock = asyncio.Lock()

        # Stats
        self._total_calls = 0
        self._total_failures = 0
        self._total_blocked = 0

    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        return self._state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (allowing requests)."""
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        return self._state == CircuitState.OPEN

    async def can_execute(self) -> bool:
        """
        Check if a request can be executed.
        Handles state transitions automatically.
        """
        async with self._lock:
            self._total_calls += 1

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                elapsed = time.monotonic() - self._last_state_change
                if elapsed >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    self._half_open_count = 0
                    return True
                else:
                    self._total_blocked += 1
                    return False

            if self._state == CircuitState.HALF_OPEN:
                # Allow limited requests in half-open state
                if self._half_open_count < self.half_open_requests:
                    self._half_open_count += 1
                    return True
                else:
                    self._total_blocked += 1
                    return False

            return False

    async def record_success(self) -> None:
        """Record a successful request."""
        async with self._lock:
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.half_open_requests:
                    self._transition_to(CircuitState.CLOSED)
                    self._failure_count = 0

            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed request."""
        async with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open immediately opens circuit
                self._transition_to(CircuitState.OPEN)
                self._success_count = 0

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
                    self._success_count = 0

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.monotonic()
        logger.info(f"Circuit breaker: {old_state.value} -> {new_state.value}")

    def get_retry_after(self) -> float:
        """Get seconds until circuit might allow requests."""
        if self._state != CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._last_state_change
        return max(0.0, self.recovery_timeout - elapsed)

    def get_stats(self) -> CircuitBreakerStats:
        """Get circuit breaker statistics."""
        return CircuitBreakerStats(
            state=self._state,
            failure_count=self._failure_count,
            success_count=self._success_count,
            last_failure_time=self._last_failure_time,
            last_state_change=self._last_state_change,
            total_calls=self._total_calls,
            total_failures=self._total_failures,
            total_blocked=self._total_blocked,
        )

    async def execute(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> T:
        """
        Execute a function with circuit breaker protection.
        """
        if not await self.can_execute():
            retry_after = self.get_retry_after()
            raise CircuitOpenError(retry_after)

        try:
            result = await func(*args, **kwargs)
            await self.record_success()
            return result
        except Exception as e:
            await self.record_failure()
            raise


# Global instance
_circuit_breaker: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> CircuitBreaker:
    """Get global circuit breaker instance."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker()
    return _circuit_breaker


def reset_circuit_breaker() -> None:
    """Reset the global circuit breaker (for testing)."""
    global _circuit_breaker
    _circuit_breaker = None
