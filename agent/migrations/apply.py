"""Apply a migration file to the configured database (DATABASE_URL / DATABASE_POOLER_URL).

    python agent/migrations/apply.py 002_documents_pgvector.sql

The migrations are idempotent, so applying one twice is safe.
"""
import asyncio
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

from backend.db.connection import close_db_pool, get_db_pool  # noqa: E402


async def apply(name: str) -> None:
    sql = (AGENT_DIR / "migrations" / name).read_text(encoding="utf-8")
    pool = await get_db_pool()
    if not pool:
        raise SystemExit("Could not connect to the database.")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
        print(f"Applied {name}.")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    asyncio.run(apply(sys.argv[1]))
