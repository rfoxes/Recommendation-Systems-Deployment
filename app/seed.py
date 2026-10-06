"""Load the sample campaigns, ad sets and variants into the database.

Insert-only: documents that already exist are left untouched, so re-running it never overwrites
edits made through the API (or resets campaign versions, which order cache writes).
Usage: python -m app.seed
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, TypeAdapter
from pymongo import UpdateOne

from app.config import get_settings
from app.db import AD_SETS, AD_VARIANTS, CAMPAIGNS, Database, create_mongo_client, ensure_indexes, to_document
from app.models import AdSet, AdVariant, Campaign

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# (collection, seed file, model, id field)
SEEDS: list[tuple[str, str, type[BaseModel], str]] = [
    (CAMPAIGNS, "campaigns.json", Campaign, "campaign_id"),
    (AD_SETS, "ad_sets.json", AdSet, "ad_set_id"),
    (AD_VARIANTS, "ad_variants.json", AdVariant, "variant_id"),
]


def load_seed_file[M: BaseModel](filename: str, model: type[M]) -> list[M]:
    return TypeAdapter(list[model]).validate_json((DATA_DIR / filename).read_bytes())


async def seed(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for collection, filename, model, id_field in SEEDS:
        items = load_seed_file(filename, model)
        ops = [
            UpdateOne({"_id": getattr(item, id_field)}, {"$setOnInsert": to_document(item, id_field)}, upsert=True)
            for item in items
        ]
        await db[collection].bulk_write(ops)
        counts[collection] = len(items)
    return counts


async def main() -> None:
    settings = get_settings()
    client = create_mongo_client(settings)
    try:
        db = client[settings.mongo_db]
        await ensure_indexes(db)
        counts = await seed(db)
        print(f"Seeded {settings.mongo_db}: {counts}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
