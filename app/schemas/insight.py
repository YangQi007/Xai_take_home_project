"""Insight schemas."""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class TrendWindow(str, Enum):
    """Time window for trend aggregation."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"


class InsightResponse(BaseModel):
    """Schema for insight in API responses."""

    id: UUID
    conversation_id: UUID
    sentiment: str
    sentiment_score: float = Field(..., ge=-1.0, le=1.0)
    topics: List[Dict[str, Any]]
    gaps: List[Dict[str, Any]]
    summary: str
    analyzed_at: datetime
    token_count: int
    cost_estimate: float

    model_config = {"from_attributes": True}


class InsightFilters(BaseModel):
    """Query filters for insights."""

    sentiment: Optional[str] = Field(None, description="Filter by sentiment")
    topic: Optional[str] = Field(None, description="Filter by topic")
    start_date: Optional[datetime] = Field(None, description="Start of date range")
    end_date: Optional[datetime] = Field(None, description="End of date range")
    limit: int = Field(50, ge=1, le=500, description="Number of results")
    offset: int = Field(0, ge=0, description="Offset for pagination")


class SentimentDistribution(BaseModel):
    """Sentiment distribution in trends."""

    positive: float = Field(..., ge=0.0, le=100.0)
    neutral: float = Field(..., ge=0.0, le=100.0)
    negative: float = Field(..., ge=0.0, le=100.0)


class TopicTrend(BaseModel):
    """Topic with trend information."""

    topic: str
    count: int
    trend: str = Field(..., description="up, down, or stable")
    change_percent: float


class VolumePoint(BaseModel):
    """Volume data point for time series."""

    timestamp: datetime
    count: int


class EmergingGap(BaseModel):
    """Emerging service gap information."""

    gap: str
    frequency: int
    first_seen: datetime
    trend: str


class TrendsResponse(BaseModel):
    """Aggregated trends response."""

    window: TrendWindow
    start_date: datetime
    end_date: datetime
    sentiment_distribution: SentimentDistribution
    sentiment_drift: float = Field(..., description="Change in avg sentiment")
    top_topics: List[TopicTrend]
    volume_over_time: List[VolumePoint]
    emerging_gaps: List[EmergingGap]
    total_conversations: int
    total_analyzed: int


class PaginatedInsights(BaseModel):
    """Paginated insights response."""

    items: List[InsightResponse]
    total: int
    limit: int
    offset: int
    has_more: bool
