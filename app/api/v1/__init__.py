"""API v1 routes."""
from fastapi import APIRouter

from app.api.v1 import conversations, insights, trends

router = APIRouter(prefix="/api/v1")

router.include_router(conversations.router, tags=["conversations"])
router.include_router(insights.router, tags=["insights"])
router.include_router(trends.router, tags=["trends"])
