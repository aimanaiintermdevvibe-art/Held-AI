import asyncio
import logging
import os
from typing import Optional
import asyncpg
from backend.core.config import get_settings

logger = logging.getLogger("held_ai.db")

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


async def get_db_pool() -> Optional[asyncpg.Pool]:
    """Get or create the asyncpg database connection pool with automatic fallback."""
    global _pool
    if _pool is not None:
        return _pool

    async with _pool_lock:
        if _pool is not None:
            return _pool

        settings = get_settings()
        candidates = [
            settings.database_url,
            os.environ.get("DATABASE_URL", ""),
            settings.database_pooler_url,
            os.environ.get("DATABASE_POOLER_URL", ""),
        ]

        # Filter out empty or duplicate candidates
        urls = []
        for u in candidates:
            if u and u not in urls:
                urls.append(u)

        for url in urls:
            try:
                # This service connects to both TLS-only hosted databases and
                # the Held LAN PostgreSQL server, which does not support TLS.
                pool = await asyncpg.create_pool(
                    dsn=url,
                    ssl="prefer",
                    min_size=1,
                    max_size=10,
                    command_timeout=15,
                )
                # Verify pool by running a test query
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1;")
                _pool = pool
                logger.info("Successfully connected database pool via %s", url.split("@")[-1])
                return _pool
            except Exception as exc:
                logger.warning("Failed connecting to database candidate: %s", exc)

        logger.error("Could not initialize database connection pool to any candidate URL.")
        return None


async def close_db_pool():
    """Close the global database pool on application shutdown."""
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception as exc:
            logger.warning("Error closing database pool: %s", exc)
        finally:
            _pool = None