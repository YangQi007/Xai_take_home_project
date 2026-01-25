"""Grok API client with retry logic and cost tracking."""
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class GrokResponse:
    """Structured response from Grok API."""

    content: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_estimate: float


class GrokClientError(Exception):
    """Base exception for Grok client errors."""

    pass


class GrokRateLimitError(GrokClientError):
    """Rate limit exceeded."""

    def __init__(self, retry_after: Optional[float] = None):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after: {retry_after}s")


class GrokAPIError(GrokClientError):
    """General API error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Grok API error {status_code}: {message}")


class GrokClient:
    """Async client for Grok API with resilience patterns."""

    ANALYSIS_PROMPT_TEMPLATE = """Analyze this customer support conversation thread:

{conversation_text}

Provide a JSON response with exactly this structure:
{{
    "sentiment": "positive" | "negative" | "neutral",
    "sentiment_score": <float from -1.0 to 1.0>,
    "topics": [
        {{"name": "<topic>", "relevance": <float 0-1>}}
    ],
    "gaps": [
        {{"description": "<service gap or issue>", "severity": "low" | "medium" | "high"}}
    ],
    "summary": "<1-2 sentence summary of the conversation>"
}}

Only return valid JSON, no additional text."""

    def __init__(self):
        self.base_url = settings.grok_api_base_url
        self.api_key = settings.xai_api_key
        self.model = settings.grok_model
        self.timeout = settings.grok_timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate API cost based on token usage."""
        input_cost = (input_tokens / 1000) * settings.grok_input_cost_per_1k
        output_cost = (output_tokens / 1000) * settings.grok_output_cost_per_1k
        return input_cost + output_cost

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    )
    async def _make_request(self, messages: list[Dict[str, str]]) -> Dict[str, Any]:
        """Make API request with retry logic."""
        client = await self._get_client()

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 1024,
        }

        response = await client.post("/chat/completions", json=payload)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise GrokRateLimitError(
                retry_after=float(retry_after) if retry_after else None
            )

        if response.status_code >= 400:
            raise GrokAPIError(response.status_code, response.text)

        return response.json()

    async def analyze_conversation(
        self, conversation_text: str
    ) -> GrokResponse:
        """Analyze a conversation and return structured insights."""
        prompt = self.ANALYSIS_PROMPT_TEMPLATE.format(
            conversation_text=conversation_text
        )

        messages = [
            {
                "role": "system",
                "content": "You are a customer support analyst. Analyze conversations and provide structured insights in JSON format.",
            },
            {"role": "user", "content": prompt},
        ]

        response_data = await self._make_request(messages)

        # Extract response content
        content = response_data["choices"][0]["message"]["content"]
        usage = response_data.get("usage", {})

        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

        return GrokResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_estimate=self._estimate_cost(input_tokens, output_tokens),
        )

    def parse_analysis_response(self, response: GrokResponse) -> Dict[str, Any]:
        """Parse the JSON response from Grok."""
        try:
            # Try to extract JSON from the response
            content = response.content.strip()

            # Handle potential markdown code blocks
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1])

            result = json.loads(content)

            # Validate required fields
            required_fields = ["sentiment", "sentiment_score", "topics", "gaps", "summary"]
            for field in required_fields:
                if field not in result:
                    raise ValueError(f"Missing required field: {field}")

            # Normalize sentiment
            sentiment = result["sentiment"].lower()
            if sentiment not in ("positive", "negative", "neutral"):
                sentiment = "neutral"
            result["sentiment"] = sentiment

            # Clamp sentiment score
            result["sentiment_score"] = max(-1.0, min(1.0, float(result["sentiment_score"])))

            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Grok response: {e}")
            # Return default analysis on parse failure
            return {
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "topics": [],
                "gaps": [],
                "summary": "Analysis failed to parse.",
            }


# Global client instance
_grok_client: Optional[GrokClient] = None


def get_grok_client() -> GrokClient:
    """Get global Grok client instance."""
    global _grok_client
    if _grok_client is None:
        _grok_client = GrokClient()
    return _grok_client


async def close_grok_client() -> None:
    """Close global Grok client."""
    global _grok_client
    if _grok_client:
        await _grok_client.close()
        _grok_client = None
