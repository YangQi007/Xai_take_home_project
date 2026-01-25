"""Conversation and Tweet models."""
import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.insight import Insight


class ConversationStatus(enum.Enum):
    """Status of conversation analysis."""

    PENDING = "pending"
    PROCESSING = "processing"
    ANALYZED = "analyzed"
    FAILED = "failed"


class Tweet(Base):
    """Raw tweet storage."""

    __tablename__ = "tweets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    author_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    inbound: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Relationships
    parent_relationships: Mapped[List["TweetRelationship"]] = relationship(
        "TweetRelationship",
        foreign_keys="TweetRelationship.tweet_id",
        back_populates="tweet",
        cascade="all, delete-orphan",
    )
    child_relationships: Mapped[List["TweetRelationship"]] = relationship(
        "TweetRelationship",
        foreign_keys="TweetRelationship.parent_tweet_id",
        back_populates="parent_tweet",
    )

    __table_args__ = (
        Index("ix_tweets_inbound_created", "inbound", "created_at"),
    )


class TweetRelationship(Base):
    """Thread structure relationships between tweets."""

    __tablename__ = "tweet_relationships"

    tweet_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tweets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_tweet_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tweets.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Relationships
    tweet: Mapped["Tweet"] = relationship(
        "Tweet",
        foreign_keys=[tweet_id],
        back_populates="parent_relationships",
    )
    parent_tweet: Mapped["Tweet"] = relationship(
        "Tweet",
        foreign_keys=[parent_tweet_id],
        back_populates="child_relationships",
    )


class Conversation(Base):
    """Reconstructed conversation threads."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    root_tweet_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tweets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    participant_count: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus),
        default=ConversationStatus.PENDING,
        index=True,
    )
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Relationships
    root_tweet: Mapped["Tweet"] = relationship("Tweet", foreign_keys=[root_tweet_id])
    insights: Mapped[List["Insight"]] = relationship(
        "Insight",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_conversations_status_created", "status", "created_at"),
    )
