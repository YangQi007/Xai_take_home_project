"""Conversation and Tweet schemas."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TweetCreate(BaseModel):
    """Schema for creating a tweet."""

    tweet_id: int = Field(..., description="Unique tweet identifier")
    author_id: str = Field(..., max_length=64, description="Author identifier")
    inbound: bool = Field(..., description="True if customer message, False if company")
    created_at: datetime = Field(..., description="Tweet creation timestamp")
    text: str = Field(..., min_length=1, description="Tweet content")
    response_tweet_id: Optional[str] = Field(
        None,
        description="Comma-separated IDs of tweets that respond to this one",
    )
    in_response_to_tweet_id: Optional[int] = Field(
        None,
        description="Parent tweet ID if this is a reply",
    )


class TweetResponse(BaseModel):
    """Schema for tweet in API responses."""

    tweet_id: int
    author_id: str
    inbound: bool
    created_at: datetime
    text: str

    model_config = {"from_attributes": True}


class ConversationCreate(BaseModel):
    """Schema for creating a single conversation."""

    tweets: List[TweetCreate] = Field(
        ...,
        min_length=1,
        description="List of tweets forming the conversation",
    )

    @field_validator("tweets")
    @classmethod
    def validate_tweets(cls, v: List[TweetCreate]) -> List[TweetCreate]:
        """Ensure tweets form a valid conversation structure."""
        if not v:
            raise ValueError("Conversation must have at least one tweet")
        return v


class ConversationResponse(BaseModel):
    """Schema for conversation in API responses."""

    id: UUID
    root_tweet_id: int
    participant_count: int
    message_count: int
    created_at: datetime
    updated_at: datetime
    status: str
    tweets: Optional[List[TweetResponse]] = None

    model_config = {"from_attributes": True}


class ConversationBulkCreate(BaseModel):
    """Schema for bulk conversation submission."""

    conversations: List[ConversationCreate] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="List of conversations (max 500)",
    )


class ConversationBulkItem(BaseModel):
    """Single item in bulk response."""

    conversation_id: Optional[UUID] = None
    status: str
    error: Optional[str] = None


class ConversationBulkResponse(BaseModel):
    """Response for bulk conversation submission."""

    total: int
    accepted: int
    rejected: int
    items: List[ConversationBulkItem]
