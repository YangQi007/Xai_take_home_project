"""Conversation API endpoints."""
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Conversation, Tweet, TweetRelationship
from app.processing import get_processing_queue
from app.schemas.conversation import (
    ConversationBulkCreate,
    ConversationBulkResponse,
    ConversationBulkItem,
    ConversationCreate,
    ConversationResponse,
    TweetResponse,
)
from app.services.ingestion import get_ingestion_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a single conversation",
)
async def create_conversation(
    conversation: ConversationCreate,
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    """
    Submit a single conversation for analysis.

    The conversation will be queued for processing by background workers.
    """
    ingestion_service = get_ingestion_service()
    queue = get_processing_queue()

    try:
        conv_obj = await ingestion_service.ingest_conversation(db, conversation)
        await db.commit()

        # Queue for analysis
        if not await queue.enqueue(conv_obj.id):
            logger.warning(f"Queue full, conversation {conv_obj.id} not queued")

        return ConversationResponse(
            id=conv_obj.id,
            root_tweet_id=conv_obj.root_tweet_id,
            participant_count=conv_obj.participant_count,
            message_count=conv_obj.message_count,
            created_at=conv_obj.created_at,
            updated_at=conv_obj.updated_at,
            status=conv_obj.status.value,
        )

    except Exception as e:
        logger.error(f"Failed to create conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )


@router.post(
    "/conversations/bulk",
    response_model=ConversationBulkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit multiple conversations",
)
async def create_conversations_bulk(
    bulk: ConversationBulkCreate,
    db: AsyncSession = Depends(get_db),
) -> ConversationBulkResponse:
    """
    Submit up to 500 conversations for analysis.

    Returns status for each conversation (accepted or rejected).
    """
    ingestion_service = get_ingestion_service()
    queue = get_processing_queue()

    results = await ingestion_service.ingest_bulk(db, bulk.conversations)
    await db.commit()

    items = []
    accepted = 0
    rejected = 0

    for conv_id, conv_status, error in results:
        items.append(
            ConversationBulkItem(
                conversation_id=conv_id,
                status=conv_status,
                error=error,
            )
        )
        if conv_status == "accepted":
            accepted += 1
            # Queue for analysis
            if conv_id and not await queue.enqueue(conv_id):
                logger.warning(f"Queue full, conversation {conv_id} not queued")
        else:
            rejected += 1

    return ConversationBulkResponse(
        total=len(results),
        accepted=accepted,
        rejected=rejected,
        items=items,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    summary="Get a conversation by ID",
)
async def get_conversation(
    conversation_id: UUID,
    include_tweets: bool = Query(False, description="Include all tweets"),
    db: AsyncSession = Depends(get_db),
) -> ConversationResponse:
    """
    Get a conversation by its ID, optionally including all tweets.
    """
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = result.scalar_one_or_none()

    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {conversation_id} not found",
        )

    tweets = None
    if include_tweets:
        tweets = await _get_conversation_tweets(db, conversation.root_tweet_id)

    return ConversationResponse(
        id=conversation.id,
        root_tweet_id=conversation.root_tweet_id,
        participant_count=conversation.participant_count,
        message_count=conversation.message_count,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        status=conversation.status.value,
        tweets=tweets,
    )


@router.get(
    "/conversations",
    response_model=List[ConversationResponse],
    summary="List conversations",
)
async def list_conversations(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> List[ConversationResponse]:
    """
    List conversations with optional status filtering.
    """
    query = select(Conversation).order_by(Conversation.created_at.desc())

    if status_filter:
        from app.models import ConversationStatus
        try:
            status_enum = ConversationStatus(status_filter)
            query = query.where(Conversation.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status_filter}",
            )

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    conversations = result.scalars().all()

    return [
        ConversationResponse(
            id=c.id,
            root_tweet_id=c.root_tweet_id,
            participant_count=c.participant_count,
            message_count=c.message_count,
            created_at=c.created_at,
            updated_at=c.updated_at,
            status=c.status.value,
        )
        for c in conversations
    ]


async def _get_conversation_tweets(
    db: AsyncSession, root_tweet_id: int
) -> List[TweetResponse]:
    """Get all tweets in a conversation thread."""
    tweets = []
    visited = set()

    async def collect(tweet_id: int):
        if tweet_id in visited:
            return
        visited.add(tweet_id)

        result = await db.execute(select(Tweet).where(Tweet.id == tweet_id))
        tweet = result.scalar_one_or_none()
        if tweet:
            tweets.append(tweet)

            # Get children
            children = await db.execute(
                select(TweetRelationship.tweet_id).where(
                    TweetRelationship.parent_tweet_id == tweet_id
                )
            )
            for (child_id,) in children:
                await collect(child_id)

    await collect(root_tweet_id)

    # Sort by created_at
    tweets.sort(key=lambda t: t.created_at)

    return [
        TweetResponse(
            tweet_id=t.id,
            author_id=t.author_id,
            inbound=t.inbound,
            created_at=t.created_at,
            text=t.text,
        )
        for t in tweets
    ]
