"""Copy Held's mood catalogue (GET {HELD_API_BASE_URL}/moods) into public."Mood".

    python agent/scripts/seed_moods.py

Run migration 005_grief_notes.sql first. Safe to re-run: moods are inserted or updated by
their Held id, and nothing is deleted.
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.db import grief_notes  # noqa: E402
from backend.db.connection import close_db_pool  # noqa: E402


def fetch_moods(base_url: str, http: httpx.Client | None = None) -> list[dict]:
    """Held's active moods; the endpoint is public, so no token is needed."""
    http = http or httpx.Client(timeout=15.0)
    res = http.get(f"{base_url.rstrip('/')}/moods")
    res.raise_for_status()
    data = res.json().get("data")
    if not isinstance(data, list):
        raise ValueError("Held /moods did not return a list.")
    moods = [
        {"id": m["id"], "code": m["code"], "title": m["title"], "imageUrl": m.get("imageUrl")}
        for m in data
        if isinstance(m, dict) and all(isinstance(m.get(k), str) and m[k] for k in ("id", "code", "title"))
    ]
    if not moods:
        raise ValueError("Held /moods returned no moods; nothing was changed.")
    return moods


async def seed() -> None:
    base_url = get_settings().held_api_base_url
    moods = fetch_moods(base_url)
    try:
        result = await grief_notes.upsert_moods(moods)
        stored = await grief_notes.list_moods()
    finally:
        await close_db_pool()
    print(f"Fetched {len(moods)} moods from {base_url}/moods.")
    print(f"Added {result['added']}, updated {result['updated']}, unchanged {result['unchanged']}.")
    print(f'public."Mood" now holds {len(stored)} moods.')


if __name__ == "__main__":
    asyncio.run(seed())
