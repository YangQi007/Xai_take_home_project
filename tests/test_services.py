"""Tests for service layer."""
import pytest
from datetime import datetime, timedelta

from app.services.cache import CacheService
from app.services.grok_client import GrokClient, GrokResponse
from app.utils.thread_builder import ThreadBuilder, ConversationThread


class TestCacheService:
    """Tests for CacheService."""

    def test_compute_hash_normalizes_text(self):
        """Hash should be consistent for normalized text."""
        cache = CacheService()

        text1 = "Hello   World"
        text2 = "hello world"
        text3 = "HELLO WORLD"

        hash1 = cache.compute_hash(text1)
        hash2 = cache.compute_hash(text2)
        hash3 = cache.compute_hash(text3)

        # All should produce same hash after normalization
        assert hash1 == hash2 == hash3

    def test_compute_hash_different_for_different_text(self):
        """Different text should produce different hashes."""
        cache = CacheService()

        hash1 = cache.compute_hash("Hello World")
        hash2 = cache.compute_hash("Goodbye World")

        assert hash1 != hash2


class TestGrokClient:
    """Tests for GrokClient."""

    def test_parse_analysis_response_valid_json(self):
        """Should parse valid JSON response."""
        client = GrokClient()
        response = GrokResponse(
            content='{"sentiment": "positive", "sentiment_score": 0.8, "topics": [{"name": "support"}], "gaps": [], "summary": "Good conversation"}',
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_estimate=0.001,
        )

        result = client.parse_analysis_response(response)

        assert result["sentiment"] == "positive"
        assert result["sentiment_score"] == 0.8
        assert len(result["topics"]) == 1
        assert result["summary"] == "Good conversation"

    def test_parse_analysis_response_with_markdown(self):
        """Should handle markdown code blocks."""
        client = GrokClient()
        response = GrokResponse(
            content='```json\n{"sentiment": "negative", "sentiment_score": -0.5, "topics": [], "gaps": [], "summary": "Bad experience"}\n```',
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_estimate=0.001,
        )

        result = client.parse_analysis_response(response)

        assert result["sentiment"] == "negative"
        assert result["sentiment_score"] == -0.5

    def test_parse_analysis_response_clamps_score(self):
        """Should clamp sentiment score to [-1, 1]."""
        client = GrokClient()
        response = GrokResponse(
            content='{"sentiment": "positive", "sentiment_score": 1.5, "topics": [], "gaps": [], "summary": "Test"}',
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_estimate=0.001,
        )

        result = client.parse_analysis_response(response)

        assert result["sentiment_score"] == 1.0

    def test_parse_analysis_response_invalid_json(self):
        """Should return default on invalid JSON."""
        client = GrokClient()
        response = GrokResponse(
            content='not valid json',
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_estimate=0.001,
        )

        result = client.parse_analysis_response(response)

        assert result["sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0


class TestThreadBuilder:
    """Tests for ThreadBuilder."""

    def test_build_simple_thread(self):
        """Should build a simple two-message thread."""
        builder = ThreadBuilder()
        now = datetime.utcnow()

        builder.add_tweet(1, "customer", True, now, "Help me!")
        builder.add_tweet(2, "support", False, now + timedelta(minutes=5), "Sure!", parent_id=1)

        threads = builder.build_threads()

        assert len(threads) == 1
        thread = threads[0]
        assert thread.root_id == 1
        assert thread.message_count == 2
        assert thread.participant_count == 2
        assert len(thread.tweets) == 2

    def test_build_branching_thread(self):
        """Should handle threads with multiple replies."""
        builder = ThreadBuilder()
        now = datetime.utcnow()

        builder.add_tweet(1, "customer", True, now, "Question?")
        builder.add_tweet(2, "support", False, now + timedelta(minutes=1), "Answer 1", parent_id=1)
        builder.add_tweet(3, "support", False, now + timedelta(minutes=2), "Answer 2", parent_id=1)
        builder.add_tweet(4, "customer", True, now + timedelta(minutes=3), "Thanks!", parent_id=2)

        threads = builder.build_threads()

        assert len(threads) == 1
        assert threads[0].message_count == 4

    def test_build_multiple_threads(self):
        """Should build multiple independent threads."""
        builder = ThreadBuilder()
        now = datetime.utcnow()

        # Thread 1
        builder.add_tweet(1, "customer1", True, now, "Question 1")
        builder.add_tweet(2, "support", False, now + timedelta(minutes=1), "Answer 1", parent_id=1)

        # Thread 2
        builder.add_tweet(3, "customer2", True, now, "Question 2")
        builder.add_tweet(4, "support", False, now + timedelta(minutes=1), "Answer 2", parent_id=3)

        threads = builder.build_threads()

        assert len(threads) == 2
        assert all(t.message_count == 2 for t in threads)

    def test_chronological_ordering(self):
        """Should order tweets chronologically."""
        builder = ThreadBuilder()
        now = datetime.utcnow()

        # Add in non-chronological order (all in same thread)
        builder.add_tweet(3, "customer", True, now + timedelta(minutes=10), "Third", parent_id=2)
        builder.add_tweet(1, "customer", True, now, "First")
        builder.add_tweet(2, "support", False, now + timedelta(minutes=5), "Second", parent_id=1)

        threads = builder.build_threads()
        thread = threads[0]

        assert thread.tweets[0].text == "First"
        assert thread.tweets[1].text == "Second"
        assert thread.tweets[2].text == "Third"
