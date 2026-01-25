"""Adaptive rate limiter that adjusts based on API responses."""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class RateLimitStats:
    """Statistics for rate limiter."""

    current_rate: float
    min_rate: float
    max_rate: float
    success_streak: int
    total_requests: int
    total_throttled: int
    total_adaptations: int


class AdaptiveRateLimiter:
    """
    Token bucket rate limiter with adaptive rate adjustment.

    - Grows rate on success streaks
    - Shrinks rate on errors or latency spikes
    """

    def __init__(
        self,
        initial_rate: Optional[float] = None,
        min_rate: float = 1.0,
        max_rate: float = 50.0,
        grow_factor: float = 1.2,
        shrink_factor: float = 0.5,
        success_streak_threshold: int = 10,
    ):
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.grow_factor = grow_factor
        self.shrink_factor = shrink_factor
        self.success_streak_threshold = success_streak_threshold

        # Current state
        self._current_rate = initial_rate or settings.batch_size_initial
        self._tokens = self._current_rate
        self._last_refill = time.monotonic()
        self._success_streak = 0
        self._lock = asyncio.Lock()

        # Stats
        self._total_requests = 0
        self._total_throttled = 0
        self._total_adaptations = 0

    @property
    def current_rate(self) -> float:
        """Current tokens per second."""
        return self._current_rate

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire a token. Blocks until available or timeout.
        Returns True if acquired, False if timed out.
        """
        start_time = time.monotonic()

        while True:
            async with self._lock:
                self._refill()

                if self._tokens >= 1:
                    self._tokens -= 1
                    self._total_requests += 1
                    return True

            # Check timeout
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    self._total_throttled += 1
                    return False

            # Wait for token refill
            wait_time = 1.0 / self._current_rate
            if timeout is not None:
                remaining = timeout - (time.monotonic() - start_time)
                wait_time = min(wait_time, remaining)

            await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._current_rate,  # Max burst = rate
            self._tokens + elapsed * self._current_rate,
        )
        self._last_refill = now

    def record_success(self) -> None:
        """Record a successful request."""
        self._success_streak += 1

        if self._success_streak >= self.success_streak_threshold:
            self._grow_rate()
            self._success_streak = 0

    def record_failure(self, severity: float = 1.0) -> None:
        """
        Record a failed request.
        severity: 0.0-1.0, higher means more aggressive shrinkage.
        """
        self._success_streak = 0
        self._shrink_rate(severity)

    def record_latency_spike(self, latency: float, threshold: float = 5.0) -> None:
        """Record high latency, possibly reducing rate."""
        if latency > threshold:
            severity = min(1.0, (latency - threshold) / threshold)
            self._shrink_rate(severity * 0.5)  # Less aggressive than failures

    def _grow_rate(self) -> None:
        """Increase the rate."""
        old_rate = self._current_rate
        self._current_rate = min(
            self.max_rate,
            self._current_rate * self.grow_factor,
        )
        if self._current_rate != old_rate:
            self._total_adaptations += 1
            logger.info(f"Rate limiter: grew rate {old_rate:.2f} -> {self._current_rate:.2f}")

    def _shrink_rate(self, severity: float = 1.0) -> None:
        """Decrease the rate."""
        old_rate = self._current_rate
        factor = self.shrink_factor ** severity
        self._current_rate = max(
            self.min_rate,
            self._current_rate * factor,
        )
        if self._current_rate != old_rate:
            self._total_adaptations += 1
            logger.info(f"Rate limiter: shrunk rate {old_rate:.2f} -> {self._current_rate:.2f}")

    def force_rate(self, rate: float) -> None:
        """Force a specific rate (e.g., from Retry-After header)."""
        self._current_rate = max(self.min_rate, min(self.max_rate, rate))
        self._total_adaptations += 1
        logger.info(f"Rate limiter: forced rate to {self._current_rate:.2f}")

    def get_stats(self) -> RateLimitStats:
        """Get rate limiter statistics."""
        return RateLimitStats(
            current_rate=self._current_rate,
            min_rate=self.min_rate,
            max_rate=self.max_rate,
            success_streak=self._success_streak,
            total_requests=self._total_requests,
            total_throttled=self._total_throttled,
            total_adaptations=self._total_adaptations,
        )


# Global instance
_rate_limiter: Optional[AdaptiveRateLimiter] = None


def get_rate_limiter() -> AdaptiveRateLimiter:
    """Get global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = AdaptiveRateLimiter()
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (for testing)."""
    global _rate_limiter
    _rate_limiter = None
