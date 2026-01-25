"""Analysis orchestration service with two-stage filtering."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Conversation, ConversationStatus, Insight, Tweet, TweetRelationship
from app.services.cache import CacheService, get_cache_service
from app.services.grok_client import (
    GrokClient,
    GrokClientError,
    get_grok_client,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class AnalysisService:
    """Service for analyzing conversations with two-stage filtering."""

    # Pre-filter thresholds
    MIN_MESSAGE_COUNT = 2  # Skip single-message "conversations"
    MIN_TEXT_LENGTH = 50  # Skip very short conversations
    MAX_TEXT_LENGTH = 10000  # Truncate very long conversations

    def __init__(
        self,
        grok_client: Optional[GrokClient] = None,
        cache_service: Optional[CacheService] = None,
    ):
        self.grok_client = grok_client or get_grok_client()
        self.cache_service = cache_service or get_cache_service()

    async def get_conversation_text(
        self, db: AsyncSession, conversation_id: UUID
    ) -> Tuple[str, List[Tweet]]:
        """Reconstruct conversation text from tweets."""
        # Get conversation with root tweet
        conv_result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conversation = conv_result.scalar_one_or_none()
        if not conversation:
            raise ValueError(f"Conversation {conversation_id} not found")

        # Get all tweets in the conversation thread
        tweets = await self._get_thread_tweets(db, conversation.root_tweet_id)

        # Sort by created_at for chronological order
        tweets.sort(key=lambda t: t.created_at)

        # Build conversation text
        lines = []
        for tweet in tweets:
            role = "Customer" if tweet.inbound else "Support"
            lines.append(f"[{role}] {tweet.text}")

        return "\n\n".join(lines), tweets

    async def _get_thread_tweets(
        self, db: AsyncSession, root_tweet_id: int
    ) -> List[Tweet]:
        """Recursively get all tweets in a thread."""
        tweets = []
        visited = set()

        async def collect_thread(tweet_id: int):
            if tweet_id in visited:
                return
            visited.add(tweet_id)

            # Get the tweet
            result = await db.execute(
                select(Tweet).where(Tweet.id == tweet_id)
            )
            tweet = result.scalar_one_or_none()
            if tweet:
                tweets.append(tweet)

                # Get child tweets (responses)
                children_result = await db.execute(
                    select(TweetRelationship.tweet_id).where(
                        TweetRelationship.parent_tweet_id == tweet_id
                    )
                )
                for (child_id,) in children_result:
                    await collect_thread(child_id)

        await collect_thread(root_tweet_id)
        return tweets

    def should_analyze(
        self, tweets: List[Tweet], conversation_text: str
    ) -> Tuple[bool, str]:
        """
        First-stage cheap filter to decide if conversation is worth analyzing.
        Returns (should_analyze, reason).
        """
        # Check message count
        if len(tweets) < self.MIN_MESSAGE_COUNT:
            return False, "too_few_messages"

        # Check text length
        if len(conversation_text) < self.MIN_TEXT_LENGTH:
            return False, "text_too_short"

        # Check for meaningful customer interaction
        has_customer = any(t.inbound for t in tweets)
        has_support = any(not t.inbound for t in tweets)

        if not has_customer:
            return False, "no_customer_message"

        if not has_support:
            return False, "no_support_response"

        return True, "eligible"

    async def analyze_conversation(
        self, db: AsyncSession, conversation_id: UUID
    ) -> Optional[Insight]:
        """
        Analyze a conversation with two-stage filtering.
        Returns the created Insight or None if filtered out.
        """
        # Update status to processing
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(status=ConversationStatus.PROCESSING)
        )

        try:
            # Get conversation text
            conversation_text, tweets = await self.get_conversation_text(
                db, conversation_id
            )

            # Stage 1: Cheap pre-filter
            should_analyze, reason = self.should_analyze(tweets, conversation_text)
            if not should_analyze:
                logger.info(
                    f"Skipping conversation {conversation_id}: {reason}"
                )
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(status=ConversationStatus.ANALYZED)
                )
                return None

            # Compute hash for caching
            text_hash = self.cache_service.compute_hash(conversation_text)

            # Update conversation with content hash
            await db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(content_hash=text_hash)
            )

            # Stage 2: Check cache
            cached_result = await self.cache_service.get(db, text_hash)
            if cached_result:
                logger.info(f"Cache hit for conversation {conversation_id}")
                insight = await self._create_insight(
                    db, conversation_id, cached_result, 0, 0.0
                )
                return insight

            # Truncate if needed
            if len(conversation_text) > self.MAX_TEXT_LENGTH:
                conversation_text = conversation_text[: self.MAX_TEXT_LENGTH] + "\n[Truncated]"

            # Stage 3: Expensive Grok API call
            logger.info(f"Calling Grok API for conversation {conversation_id}")
            response = await self.grok_client.analyze_conversation(conversation_text)
            parsed = self.grok_client.parse_analysis_response(response)

            # Cache the result
            await self.cache_service.set(db, text_hash, parsed)

            # Create insight
            insight = await self._create_insight(
                db,
                conversation_id,
                parsed,
                response.total_tokens,
                response.cost_estimate,
            )

            return insight

        except GrokClientError as e:
            logger.error(f"Grok API error for {conversation_id}: {e}")
            await db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(status=ConversationStatus.FAILED)
            )
            raise

        except Exception as e:
            logger.error(f"Analysis error for {conversation_id}: {e}")
            await db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(status=ConversationStatus.FAILED)
            )
            raise

    async def _create_insight(
        self,
        db: AsyncSession,
        conversation_id: UUID,
        analysis: Dict[str, Any],
        token_count: int,
        cost_estimate: float,
    ) -> Insight:
        """Create and store an insight from analysis results."""
        insight = Insight(
            conversation_id=conversation_id,
            sentiment=analysis["sentiment"],
            sentiment_score=analysis["sentiment_score"],
            topics=analysis.get("topics", []),
            gaps=analysis.get("gaps", []),
            summary=analysis.get("summary", ""),
            analyzed_at=datetime.utcnow(),
            token_count=token_count,
            cost_estimate=cost_estimate,
        )

        db.add(insight)

        # Update conversation status
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(status=ConversationStatus.ANALYZED)
        )

        await db.flush()
        return insight


# Global instance
_analysis_service: Optional[AnalysisService] = None


def get_analysis_service() -> AnalysisService:
    """Get global analysis service instance."""
    global _analysis_service
    if _analysis_service is None:
        _analysis_service = AnalysisService()
    return _analysis_service
