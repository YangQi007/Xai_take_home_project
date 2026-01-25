"""Insights API endpoints."""
import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Insight
from app.schemas.insight import InsightResponse, PaginatedInsights

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/insights",
    response_model=PaginatedInsights,
    summary="Get insights with filtering",
)
async def get_insights(
    sentiment: Optional[str] = Query(None, description="Filter by sentiment"),
    topic: Optional[str] = Query(None, description="Filter by topic (partial match)"),
    start_date: Optional[datetime] = Query(None, description="Start of date range"),
    end_date: Optional[datetime] = Query(None, description="End of date range"),
    min_score: Optional[float] = Query(None, ge=-1.0, le=1.0, description="Minimum sentiment score"),
    max_score: Optional[float] = Query(None, ge=-1.0, le=1.0, description="Maximum sentiment score"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> PaginatedInsights:
    """
    Get insights with optional filtering by sentiment, topic, date range, and score.
    """
    # Build query
    conditions = []

    if sentiment:
        sentiment_lower = sentiment.lower()
        if sentiment_lower not in ("positive", "negative", "neutral"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid sentiment: {sentiment}. Must be positive, negative, or neutral.",
            )
        conditions.append(Insight.sentiment == sentiment_lower)

    if topic:
        # JSONB contains query for topic matching
        # Escape SQL LIKE special characters to prevent injection
        escaped_topic = (
            topic.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        # Check if any topic in the array has a matching name
        # Cast JSONB to text for LIKE matching
        conditions.append(
            Insight.topics.cast(String).ilike(f"%{escaped_topic}%", escape="\\")
        )

    if start_date:
        conditions.append(Insight.analyzed_at >= start_date)

    if end_date:
        conditions.append(Insight.analyzed_at <= end_date)

    if min_score is not None:
        conditions.append(Insight.sentiment_score >= min_score)

    if max_score is not None:
        conditions.append(Insight.sentiment_score <= max_score)

    # Count total matching
    count_query = select(func.count(Insight.id))
    if conditions:
        count_query = count_query.where(and_(*conditions))
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Fetch insights
    query = select(Insight).order_by(Insight.analyzed_at.desc())
    if conditions:
        query = query.where(and_(*conditions))
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    insights = result.scalars().all()

    items = [
        InsightResponse(
            id=i.id,
            conversation_id=i.conversation_id,
            sentiment=i.sentiment,
            sentiment_score=i.sentiment_score,
            topics=i.topics,
            gaps=i.gaps,
            summary=i.summary,
            analyzed_at=i.analyzed_at,
            token_count=i.token_count,
            cost_estimate=i.cost_estimate,
        )
        for i in insights
    ]

    return PaginatedInsights(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
    )


@router.get(
    "/insights/stats",
    summary="Get insight statistics",
)
async def get_insight_stats(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Get aggregate statistics about insights.
    """
    conditions = []
    if start_date:
        conditions.append(Insight.analyzed_at >= start_date)
    if end_date:
        conditions.append(Insight.analyzed_at <= end_date)

    # Total count
    count_query = select(func.count(Insight.id))
    if conditions:
        count_query = count_query.where(and_(*conditions))
    total = (await db.execute(count_query)).scalar() or 0

    # Sentiment distribution
    sentiment_query = select(
        Insight.sentiment,
        func.count(Insight.id).label("count"),
    ).group_by(Insight.sentiment)
    if conditions:
        sentiment_query = sentiment_query.where(and_(*conditions))
    sentiment_result = await db.execute(sentiment_query)
    sentiment_counts = {row.sentiment: row.count for row in sentiment_result}

    # Average sentiment score
    avg_query = select(func.avg(Insight.sentiment_score))
    if conditions:
        avg_query = avg_query.where(and_(*conditions))
    avg_score = (await db.execute(avg_query)).scalar() or 0.0

    # Total cost
    cost_query = select(func.sum(Insight.cost_estimate))
    if conditions:
        cost_query = cost_query.where(and_(*conditions))
    total_cost = (await db.execute(cost_query)).scalar() or 0.0

    # Total tokens
    tokens_query = select(func.sum(Insight.token_count))
    if conditions:
        tokens_query = tokens_query.where(and_(*conditions))
    total_tokens = (await db.execute(tokens_query)).scalar() or 0

    return {
        "total_insights": total,
        "sentiment_distribution": {
            "positive": sentiment_counts.get("positive", 0),
            "neutral": sentiment_counts.get("neutral", 0),
            "negative": sentiment_counts.get("negative", 0),
        },
        "average_sentiment_score": round(avg_score, 4),
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
    }


@router.get(
    "/insights/conversation/{conversation_id}",
    response_model=List[InsightResponse],
    summary="Get insights for a conversation",
)
async def get_conversation_insights(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> List[InsightResponse]:
    """
    Get all insights for a specific conversation.
    """
    result = await db.execute(
        select(Insight)
        .where(Insight.conversation_id == conversation_id)
        .order_by(Insight.analyzed_at.desc())
    )
    insights = result.scalars().all()

    return [
        InsightResponse(
            id=i.id,
            conversation_id=i.conversation_id,
            sentiment=i.sentiment,
            sentiment_score=i.sentiment_score,
            topics=i.topics,
            gaps=i.gaps,
            summary=i.summary,
            analyzed_at=i.analyzed_at,
            token_count=i.token_count,
            cost_estimate=i.cost_estimate,
        )
        for i in insights
    ]


@router.get(
    "/insights/{insight_id}",
    response_model=InsightResponse,
    summary="Get a specific insight",
)
async def get_insight(
    insight_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> InsightResponse:
    """
    Get a specific insight by ID.
    """
    result = await db.execute(
        select(Insight).where(Insight.id == insight_id)
    )
    insight = result.scalar_one_or_none()

    if not insight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Insight {insight_id} not found",
        )

    return InsightResponse(
        id=insight.id,
        conversation_id=insight.conversation_id,
        sentiment=insight.sentiment,
        sentiment_score=insight.sentiment_score,
        topics=insight.topics,
        gaps=insight.gaps,
        summary=insight.summary,
        analyzed_at=insight.analyzed_at,
        token_count=insight.token_count,
        cost_estimate=insight.cost_estimate,
    )
