"""Copy Held's disease catalogue (GET {HELD_API_BASE_URL}/diseases) into public."Disease".

    python agent/scripts/seed_diseases.py

Run migration 003_diseases.sql first. Safe to re-run: entries are inserted or updated by
their Held id, and nothing is deleted.
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.db import repository  # noqa: E402
from backend.db.connection import close_db_pool  # noqa: E402


def fetch_catalogue(base_url: str, http: httpx.Client | None = None) -> list[dict]:
    """Held's disease list; the endpoint is public, so no token is needed."""
    http = http or httpx.Client(timeout=15.0)
    res = http.get(f"{base_url.rstrip('/')}/diseases")
    res.raise_for_status()
    data = res.json().get("data")
    if not isinstance(data, list):
        raise ValueError("Held /diseases did not return a list.")
    diseases = [
        {"id": d["id"], "name": d["name"].strip(), "abbreviation": d.get("abbreviation"), "imageUrl": d.get("imageUrl")}
        for d in data
        if isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("name"), str) and d["name"].strip()
    ]
    if not diseases:
        raise ValueError("Held /diseases returned no diseases; nothing was changed.")
    return diseases


async def seed() -> None:
    base_url = get_settings().held_api_base_url
    diseases = fetch_catalogue(base_url)
    try:
        result = await repository.upsert_diseases(diseases)
        stored = await repository.list_diseases()
    finally:
        await close_db_pool()
    print(f"Fetched {len(diseases)} diseases from {base_url}/diseases.")
    print(f"Added {result['added']}, updated {result['updated']}, unchanged {result['unchanged']}.")
    print(f'public."Disease" now holds {len(stored)} diseases.')


if __name__ == "__main__":
    asyncio.run(seed())
