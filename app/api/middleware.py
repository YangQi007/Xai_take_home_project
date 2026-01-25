"""API middleware for rate limiting and error handling."""
import asyncio
import logging
import time
from collections import defaultdict
from typing import Callable, Dict, Tuple

from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter middleware.

    Returns 429 Too Many Requests with Retry-After header when limit exceeded.
    """

    def __init__(self, app, requests_per_minute: int = 100, burst: int = 20):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.burst = burst
        self.window_size = 60  # 1 minute window
        self._requests: Dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _get_client_id(self, request: Request) -> str:
        """Get client identifier from request."""
        # Use X-Forwarded-For if behind proxy, otherwise use client host
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def _is_rate_limited(self, client_id: str) -> Tuple[bool, float]:
        """
        Check if client is rate limited.
        Returns (is_limited, retry_after_seconds).
        """
        async with self._lock:
            now = time.monotonic()
            window_start = now - self.window_size

            # Clean old requests
            requests = self._requests[client_id]
            requests[:] = [t for t in requests if t > window_start]

            # Check limit
            if len(requests) >= self.requests_per_minute:
                # Calculate when oldest request will expire
                oldest = min(requests)
                retry_after = oldest + self.window_size - now
                return True, max(1.0, retry_after)

            # Check burst (requests in last second)
            burst_start = now - 1.0
            burst_count = sum(1 for t in requests if t > burst_start)
            if burst_count >= self.burst:
                return True, 1.0

            # Record request
            requests.append(now)
            return False, 0.0

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with rate limiting."""
        # Skip rate limiting for metrics endpoint
        if request.url.path == "/metrics":
            return await call_next(request)

        client_id = self._get_client_id(request)
        is_limited, retry_after = await self._is_rate_limited(client_id)

        if is_limited:
            logger.warning(f"Rate limited client {client_id}")
            return Response(
                content='{"detail": "Too many requests"}',
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={
                    "Retry-After": str(int(retry_after)),
                    "X-RateLimit-Limit": str(self.requests_per_minute),
                    "Content-Type": "application/json",
                },
            )

        # Add rate limit headers to response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)

        return response


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """
    Global error handling middleware.

    Catches unhandled exceptions and returns proper JSON error responses.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with error handling."""
        try:
            return await call_next(request)
        except Exception as e:
            logger.exception(f"Unhandled error: {e}")
            return Response(
                content='{"detail": "Internal server error"}',
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                headers={"Content-Type": "application/json"},
            )
