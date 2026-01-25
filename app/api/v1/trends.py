"""Trends API endpoint for time-windowed aggregates."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Conversation, ConversationStatus, Insight
from app.schemas.insight import (
    EmergingGap,
    SentimentDistribution,
    TopicTrend,
    TrendWindow,
    TrendsResponse,
    VolumePoint,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/trends",
    response_model=TrendsResponse,
    summary="Get time-windowed trend aggregates",
)
async def get_trends(
    window: TrendWindow = Query(TrendWindow.DAY, description="Aggregation window"),
    start_date: Optional[datetime] = Query(None, description="Start of date range"),
    end_date: Optional[datetime] = Query(None, description="End of date range"),
    db: AsyncSession = Depends(get_db),
) -> TrendsResponse:
    """
    Get aggregated trends including sentiment distribution, top topics,
    volume over time, and emerging gaps.
    """
    # Default date range
    now = datetime.utcnow()
    if end_date is None:
        end_date = now
    if start_date is None:
        if window == TrendWindow.HOUR:
            start_date = end_date - timedelta(hours=24)
        elif window == TrendWindow.DAY:
            start_date = end_date - timedelta(days=7)
        else:  # WEEK
            start_date = end_date - timedelta(weeks=4)

    # Calculate previous period for drift comparison
    period_length = end_date - start_date
    prev_start = start_date - period_length
    prev_end = start_date

    # Current period insights
    current_insights = await _get_insights_in_range(db, start_date, end_date)
    prev_insights = await _get_insights_in_range(db, prev_start, prev_end)

    # Sentiment distribution
    sentiment_dist = _calculate_sentiment_distribution(current_insights)

    # Sentiment drift
    current_avg = _calculate_avg_sentiment(current_insights)
    prev_avg = _calculate_avg_sentiment(prev_insights)
    sentiment_drift = current_avg - prev_avg

    # Top topics with trends
    current_topics = _extract_topics(current_insights)
    prev_topics = _extract_topics(prev_insights)
    top_topics = _calculate_topic_trends(current_topics, prev_topics)

    # Volume over time
    volume_points = await _calculate_volume_over_time(
        db, start_date, end_date, window
    )

    # Emerging gaps
    current_gaps = _extract_gaps(current_insights)
    prev_gaps = _extract_gaps(prev_insights)
    emerging_gaps = _calculate_emerging_gaps(current_gaps, prev_gaps, start_date)

    # Total conversations
    total_conversations = await _count_conversations(db, start_date, end_date)
    total_analyzed = len(current_insights)

    return TrendsResponse(
        window=window,
        start_date=start_date,
        end_date=end_date,
        sentiment_distribution=sentiment_dist,
        sentiment_drift=round(sentiment_drift, 4),
        top_topics=top_topics[:10],  # Top 10 topics
        volume_over_time=volume_points,
        emerging_gaps=emerging_gaps[:10],  # Top 10 emerging gaps
        total_conversations=total_conversations,
        total_analyzed=total_analyzed,
    )


async def _get_insights_in_range(
    db: AsyncSession, start: datetime, end: datetime
) -> List[Insight]:
    """Get all insights within a date range."""
    result = await db.execute(
        select(Insight).where(
            and_(
                Insight.analyzed_at >= start,
                Insight.analyzed_at <= end,
            )
        )
    )
    return list(result.scalars().all())


def _calculate_sentiment_distribution(
    insights: List[Insight],
) -> SentimentDistribution:
    """Calculate sentiment percentages."""
    if not insights:
        return SentimentDistribution(positive=0, neutral=0, negative=0)

    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for insight in insights:
        sentiment = insight.sentiment.lower()
        if sentiment in counts:
            counts[sentiment] += 1

    total = len(insights)
    return SentimentDistribution(
        positive=round(counts["positive"] / total * 100, 2),
        neutral=round(counts["neutral"] / total * 100, 2),
        negative=round(counts["negative"] / total * 100, 2),
    )


def _calculate_avg_sentiment(insights: List[Insight]) -> float:
    """Calculate average sentiment score."""
    if not insights:
        return 0.0
    return sum(i.sentiment_score for i in insights) / len(insights)


def _extract_topics(insights: List[Insight]) -> Dict[str, int]:
    """Extract topic counts from insights."""
    topic_counts: Dict[str, int] = defaultdict(int)
    for insight in insights:
        if insight.topics:
            for topic in insight.topics:
                name = topic.get("name", "") if isinstance(topic, dict) else str(topic)
                if name:
                    topic_counts[name.lower()] += 1
    return topic_counts


def _calculate_topic_trends(
    current: Dict[str, int], previous: Dict[str, int]
) -> List[TopicTrend]:
    """Calculate topic trends comparing current to previous period."""
    trends = []
    for topic, count in current.items():
        prev_count = previous.get(topic, 0)
        if prev_count > 0:
            change_percent = ((count - prev_count) / prev_count) * 100
        else:
            change_percent = 100.0 if count > 0 else 0.0

        if change_percent > 5:
            trend = "up"
        elif change_percent < -5:
            trend = "down"
        else:
            trend = "stable"

        trends.append(
            TopicTrend(
                topic=topic,
                count=count,
                trend=trend,
                change_percent=round(change_percent, 2),
            )
        )

    # Sort by count descending
    trends.sort(key=lambda t: t.count, reverse=True)
    return trends


async def _calculate_volume_over_time(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    window: TrendWindow,
) -> List[VolumePoint]:
    """Calculate conversation volume over time."""
    # Determine time bucket
    if window == TrendWindow.HOUR:
        bucket_seconds = 3600
    elif window == TrendWindow.DAY:
        bucket_seconds = 86400
    else:  # WEEK
        bucket_seconds = 604800

    # Group conversations by time bucket
    result = await db.execute(
        select(
            func.date_trunc(
                "hour" if window == TrendWindow.HOUR else "day",
                Conversation.created_at,
            ).label("bucket"),
            func.count(Conversation.id).label("count"),
        )
        .where(
            and_(
                Conversation.created_at >= start,
                Conversation.created_at <= end,
            )
        )
        .group_by("bucket")
        .order_by("bucket")
    )

    points = []
    for row in result:
        points.append(
            VolumePoint(
                timestamp=row.bucket,
                count=row.count,
            )
        )

    return points


def _extract_gaps(insights: List[Insight]) -> Dict[str, List[datetime]]:
    """Extract gaps with their occurrence times."""
    gap_times: Dict[str, List[datetime]] = defaultdict(list)
    for insight in insights:
        if insight.gaps:
            for gap in insight.gaps:
                desc = gap.get("description", "") if isinstance(gap, dict) else str(gap)
                if desc:
                    gap_times[desc.lower()].append(insight.analyzed_at)
    return gap_times


def _calculate_emerging_gaps(
    current: Dict[str, List[datetime]],
    previous: Dict[str, List[datetime]],
    start_date: datetime,
) -> List[EmergingGap]:
    """Calculate emerging gaps that are new or increasing."""
    gaps = []
    for gap_desc, times in current.items():
        prev_count = len(previous.get(gap_desc, []))
        curr_count = len(times)

        if prev_count > 0:
            change = ((curr_count - prev_count) / prev_count) * 100
        else:
            change = 100.0  # New gap

        if change > 0:  # Only show increasing or new gaps
            if change > 20:
                trend = "rising"
            elif change > 0:
                trend = "emerging"
            else:
                trend = "stable"

            first_seen = min(times) if times else start_date

            gaps.append(
                EmergingGap(
                    gap=gap_desc,
                    frequency=curr_count,
                    first_seen=first_seen,
                    trend=trend,
                )
            )

    # Sort by frequency descending
    gaps.sort(key=lambda g: g.frequency, reverse=True)
    return gaps


async def _count_conversations(
    db: AsyncSession, start: datetime, end: datetime
) -> int:
    """Count total conversations in date range."""
    result = await db.execute(
        select(func.count(Conversation.id)).where(
            and_(
                Conversation.created_at >= start,
                Conversation.created_at <= end,
            )
        )
    )
    return result.scalar() or 0
