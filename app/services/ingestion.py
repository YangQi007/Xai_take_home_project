"""Data ingestion service for CSV and API submissions."""
import csv
import hashlib
import logging
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, ConversationStatus, Tweet, TweetRelationship
from app.schemas.conversation import ConversationCreate, TweetCreate

logger = logging.getLogger(__name__)


class IngestionService:
    """Service for ingesting conversation data."""

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """Compute hash for deduplication."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def parse_csv_datetime(dt_str: str) -> datetime:
        """Parse datetime from CSV format, converting to naive UTC."""
        dt = date_parser.parse(dt_str)
        # Convert to UTC and remove timezone info for database storage
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    @staticmethod
    def normalize_datetime(dt: datetime) -> datetime:
        """Convert any datetime to naive UTC for database storage."""
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    async def ingest_csv(
        self, db: AsyncSession, csv_content: str
    ) -> Tuple[int, int, List[str]]:
        """
        Ingest conversations from CSV content.
        Returns (tweets_created, conversations_created, errors).
        """
        errors = []
        tweets_data = []

        # Parse CSV
        reader = csv.DictReader(StringIO(csv_content))
        for row_num, row in enumerate(reader, start=2):
            try:
                tweet_data = self._parse_csv_row(row)
                tweets_data.append(tweet_data)
            except Exception as e:
                errors.append(f"Row {row_num}: {e}")

        if not tweets_data:
            return 0, 0, errors

        # Bulk insert tweets
        tweets_created = await self._bulk_insert_tweets(db, tweets_data)

        # Build relationships
        relationships = self._extract_relationships(tweets_data)
        await self._bulk_insert_relationships(db, relationships)

        # Create conversations from root tweets
        conversations_created = await self._create_conversations_from_roots(
            db, tweets_data
        )

        return tweets_created, conversations_created, errors

    def _parse_csv_row(self, row: Dict[str, str]) -> Dict[str, Any]:
        """Parse a single CSV row into tweet data."""
        return {
            "id": int(row["tweet_id"]),
            "author_id": row["author_id"],
            "inbound": row["inbound"].lower() == "true",
            "created_at": self.parse_csv_datetime(row["created_at"]),
            "text": row["text"],
            "text_hash": self.compute_text_hash(row["text"]),
            "response_tweet_id": row.get("response_tweet_id", ""),
            "in_response_to_tweet_id": row.get("in_response_to_tweet_id", ""),
        }

    def _extract_relationships(
        self, tweets_data: List[Dict[str, Any]]
    ) -> List[Tuple[int, int]]:
        """Extract parent-child relationships from tweet data."""
        relationships = []
        tweet_ids = {t["id"] for t in tweets_data}

        for tweet in tweets_data:
            parent_id_str = tweet.get("in_response_to_tweet_id", "")
            if parent_id_str:
                try:
                    parent_id = int(parent_id_str)
                    if parent_id in tweet_ids:
                        relationships.append((tweet["id"], parent_id))
                except ValueError:
                    pass

        return relationships

    async def _bulk_insert_tweets(
        self, db: AsyncSession, tweets_data: List[Dict[str, Any]]
    ) -> int:
        """Bulk insert tweets, ignoring duplicates."""
        if not tweets_data:
            return 0

        # Prepare tweet records (exclude relationship fields)
        tweet_records = [
            {
                "id": t["id"],
                "author_id": t["author_id"],
                "inbound": t["inbound"],
                "created_at": t["created_at"],
                "text": t["text"],
                "text_hash": t["text_hash"],
            }
            for t in tweets_data
        ]

        # Use INSERT ... ON CONFLICT DO NOTHING
        stmt = insert(Tweet).values(tweet_records)
        stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
        result = await db.execute(stmt)

        return result.rowcount or 0

    async def _bulk_insert_relationships(
        self, db: AsyncSession, relationships: List[Tuple[int, int]]
    ) -> int:
        """Bulk insert tweet relationships."""
        if not relationships:
            return 0

        records = [
            {"tweet_id": child, "parent_tweet_id": parent}
            for child, parent in relationships
        ]

        stmt = insert(TweetRelationship).values(records)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["tweet_id", "parent_tweet_id"]
        )
        result = await db.execute(stmt)

        return result.rowcount or 0

    async def _create_conversations_from_roots(
        self, db: AsyncSession, tweets_data: List[Dict[str, Any]]
    ) -> int:
        """Create conversation records for root tweets (those without parents)."""
        # Find root tweets (no in_response_to_tweet_id)
        root_tweets = [
            t for t in tweets_data
            if not t.get("in_response_to_tweet_id")
        ]

        if not root_tweets:
            return 0

        # Build conversation stats
        tweet_map = {t["id"]: t for t in tweets_data}
        conversations_created = 0

        for root in root_tweets:
            # Check if conversation already exists
            existing = await db.execute(
                select(Conversation).where(
                    Conversation.root_tweet_id == root["id"]
                )
            )
            if existing.scalar_one_or_none():
                continue

            # Count messages and participants in thread
            thread_tweets = self._collect_thread(root["id"], tweets_data)
            participants = set(t["author_id"] for t in thread_tweets)

            conversation = Conversation(
                root_tweet_id=root["id"],
                participant_count=len(participants),
                message_count=len(thread_tweets),
                created_at=root["created_at"],
                updated_at=max(t["created_at"] for t in thread_tweets),
                status=ConversationStatus.PENDING,
            )
            db.add(conversation)
            conversations_created += 1

        return conversations_created

    def _collect_thread(
        self, root_id: int, tweets_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Collect all tweets in a thread starting from root."""
        tweet_map = {t["id"]: t for t in tweets_data}
        children_map: Dict[int, List[int]] = {}

        # Build children lookup
        for tweet in tweets_data:
            parent_str = tweet.get("in_response_to_tweet_id", "")
            if parent_str:
                try:
                    parent_id = int(parent_str)
                    if parent_id not in children_map:
                        children_map[parent_id] = []
                    children_map[parent_id].append(tweet["id"])
                except ValueError:
                    pass

        # Collect thread via BFS
        thread = []
        queue = [root_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in tweet_map:
                thread.append(tweet_map[current_id])
                if current_id in children_map:
                    queue.extend(children_map[current_id])

        return thread

    async def ingest_conversation(
        self, db: AsyncSession, conversation: ConversationCreate
    ) -> Conversation:
        """Ingest a single conversation from API submission."""
        tweets = conversation.tweets

        # Convert to internal format
        tweets_data = []
        for tweet in tweets:
            tweets_data.append({
                "id": tweet.tweet_id,
                "author_id": tweet.author_id,
                "inbound": tweet.inbound,
                "created_at": self.normalize_datetime(tweet.created_at),
                "text": tweet.text,
                "text_hash": self.compute_text_hash(tweet.text),
                "response_tweet_id": tweet.response_tweet_id or "",
                "in_response_to_tweet_id": str(tweet.in_response_to_tweet_id) if tweet.in_response_to_tweet_id else "",
            })

        # Insert tweets
        await self._bulk_insert_tweets(db, tweets_data)

        # Insert relationships
        relationships = self._extract_relationships(tweets_data)
        await self._bulk_insert_relationships(db, relationships)

        # Find root tweet (first tweet without parent, or just first tweet)
        root_tweet = next(
            (t for t in tweets_data if not t.get("in_response_to_tweet_id")),
            tweets_data[0],
        )

        # Check for existing conversation
        existing = await db.execute(
            select(Conversation).where(
                Conversation.root_tweet_id == root_tweet["id"]
            )
        )
        existing_conv = existing.scalar_one_or_none()

        if existing_conv:
            # Update existing conversation
            existing_conv.message_count = len(tweets_data)
            existing_conv.participant_count = len(set(t["author_id"] for t in tweets_data))
            existing_conv.updated_at = max(t["created_at"] for t in tweets_data)
            existing_conv.status = ConversationStatus.PENDING
            return existing_conv

        # Create new conversation
        participants = set(t["author_id"] for t in tweets_data)
        conversation_obj = Conversation(
            root_tweet_id=root_tweet["id"],
            participant_count=len(participants),
            message_count=len(tweets_data),
            created_at=root_tweet["created_at"],
            updated_at=max(t["created_at"] for t in tweets_data),
            status=ConversationStatus.PENDING,
        )
        db.add(conversation_obj)
        await db.flush()

        return conversation_obj

    async def ingest_bulk(
        self, db: AsyncSession, conversations: List[ConversationCreate]
    ) -> List[Tuple[Optional[UUID], str, Optional[str]]]:
        """
        Ingest multiple conversations.
        Returns list of (conversation_id, status, error).
        """
        results = []

        for conv in conversations:
            try:
                conversation_obj = await self.ingest_conversation(db, conv)
                results.append((conversation_obj.id, "accepted", None))
            except Exception as e:
                logger.error(f"Failed to ingest conversation: {e}")
                results.append((None, "rejected", str(e)))

        return results


# Global instance
_ingestion_service: Optional[IngestionService] = None


def get_ingestion_service() -> IngestionService:
    """Get global ingestion service instance."""
    global _ingestion_service
    if _ingestion_service is None:
        _ingestion_service = IngestionService()
    return _ingestion_service
