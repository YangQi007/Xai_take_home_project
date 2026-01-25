"""Analysis caching service to avoid duplicate Grok calls."""
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AnalysisCache

logger = logging.getLogger(__name__)
settings = get_settings()


class CacheService:
    """Service for caching analysis results."""

    @staticmethod
    def compute_hash(text: str) -> str:
        """Compute SHA-256 hash of normalized text."""
        # Normalize: lowercase, remove extra whitespace
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def get(
        self, db: AsyncSession, text_hash: str
    ) -> Optional[Dict[str, Any]]:
        """Get cached result if exists and not expired."""
        result = await db.execute(
            select(AnalysisCache).where(
                AnalysisCache.text_hash == text_hash,
                AnalysisCache.expires_at > datetime.utcnow(),
            )
        )
        cache_entry = result.scalar_one_or_none()

        if cache_entry:
            # Update hit count
            await db.execute(
                update(AnalysisCache)
                .where(AnalysisCache.text_hash == text_hash)
                .values(hit_count=AnalysisCache.hit_count + 1)
            )
            logger.debug(f"Cache hit for hash {text_hash[:8]}...")
            return cache_entry.result

        return None

    async def set(
        self,
        db: AsyncSession,
        text_hash: str,
        result: Dict[str, Any],
        ttl_hours: Optional[int] = None,
    ) -> None:
        """Store analysis result in cache."""
        ttl = ttl_hours or settings.cache_ttl_hours
        expires_at = datetime.utcnow() + timedelta(hours=ttl)

        cache_entry = AnalysisCache(
            text_hash=text_hash,
            result=result,
            created_at=datetime.utcnow(),
            expires_at=expires_at,
            hit_count=0,
        )

        # Use merge to handle upsert
        await db.merge(cache_entry)
        logger.debug(f"Cached result for hash {text_hash[:8]}...")

    async def invalidate(self, db: AsyncSession, text_hash: str) -> bool:
        """Invalidate a cache entry."""
        result = await db.execute(
            select(AnalysisCache).where(AnalysisCache.text_hash == text_hash)
        )
        cache_entry = result.scalar_one_or_none()

        if cache_entry:
            await db.delete(cache_entry)
            return True
        return False

    async def cleanup_expired(self, db: AsyncSession) -> int:
        """Remove expired cache entries."""
        from sqlalchemy import delete

        result = await db.execute(
            delete(AnalysisCache).where(
                AnalysisCache.expires_at <= datetime.utcnow()
            )
        )
        return result.rowcount

    async def get_stats(self, db: AsyncSession) -> Dict[str, Any]:
        """Get cache statistics."""
        from sqlalchemy import func

        # Total entries
        total_result = await db.execute(
            select(func.count(AnalysisCache.text_hash))
        )
        total = total_result.scalar() or 0

        # Total hits
        hits_result = await db.execute(
            select(func.sum(AnalysisCache.hit_count))
        )
        total_hits = hits_result.scalar() or 0

        # Expired entries
        expired_result = await db.execute(
            select(func.count(AnalysisCache.text_hash)).where(
                AnalysisCache.expires_at <= datetime.utcnow()
            )
        )
        expired = expired_result.scalar() or 0

        return {
            "total_entries": total,
            "total_hits": total_hits,
            "expired_entries": expired,
            "active_entries": total - expired,
        }


# Global instance
_cache_service: Optional[CacheService] = None


def get_cache_service() -> CacheService:
    """Get global cache service instance."""
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService()
    return _cache_service
