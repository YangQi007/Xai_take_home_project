"""Insight and Cache models."""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Insight(Base):
    """Grok analysis results for conversations."""

    __tablename__ = "insights"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Sentiment analysis
    sentiment: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    sentiment_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Topics and gaps
    topics: Mapped[List[Dict[str, Any]]] = mapped_column(JSONB, default=list)
    gaps: Mapped[List[Dict[str, Any]]] = mapped_column(JSONB, default=list)

    # Summary
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # Metadata
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationship
    conversation: Mapped["Conversation"] = relationship(
        "Conversation",
        back_populates="insights",
    )

    __table_args__ = (
        Index("ix_insights_sentiment_analyzed", "sentiment", "analyzed_at"),
        Index("ix_insights_topics", "topics", postgresql_using="gin"),
    )


class AnalysisCache(Base):
    """Cache for avoiding duplicate Grok API calls."""

    __tablename__ = "analysis_cache"

    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    result: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


# Forward reference
from app.models.conversation import Conversation  # noqa: E402
